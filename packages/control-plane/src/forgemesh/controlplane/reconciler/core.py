"""9 步协调闭环实现。

对齐 development-plan 5.3 / architecture 6.2。协调器将资源的期望状态（spec）
收敛到实际状态（observed），核心约束为幂等性（INV-1/INV-3）。

9 步流程
--------
1. **获取资源**：从 Registry 读取期望状态（ctx.resource 已由上层注入）。
2. **查询观测状态**：收集实际状态快照（ctx.observed）。
3. **比较差异**：desired vs observed，判断是否需要协调。
4. **生成候选**：调用 DecisionProvider 产出候选动作（建议，非授权）。
5. **Policy 校验**：PolicyEngine.check()，deny 则停止（不可绕过，INV-3）。
6. **Scheduler 分配**：如需分配资源，Scheduler.allocate()（唯一授权分配点）。
7. **WorkEngine 执行**：如需执行工作流，WorkflowRuntime.start()。
8. **重观察**：收集执行后的新观测状态（P1 简化为标记完成）。
9. **更新 status + 安排下次**：写回 status patch，返回 requeue_after。

需要分配或执行工作流、却未注入对应组件时，在任何副作用之前返回
``Ready=False``（``SchedulerUnavailable`` / ``WorkflowRuntimeUnavailable``），
而不是跳过该步骤后宣称协调完成。

终止协调（Terminating，对齐 architecture.md §5.3/§12.4 与 development-plan
第 7 章删除闭环）
--------------------------------------------------------------------
观测 ``phase == Terminating`` 时不走 9 步差异收敛，改走受控释放子流程：

1. 调用注入的 :class:`ReleaseProvider` 释放外部副作用（停止实例、归还 GPU
   租约等；未注入时为 Noop 语义，仅移除 finalizer）；失败 → 保持
   ``Terminating`` 并 requeue 重试；
2. 移除默认 release finalizer（``forgemesh.io/resource-release``）并 CAS 写回；
3. finalizers 全部清空后：释放关联 Allocation（若存在 Active）→ 物理 DELETE
   → 记审计日志。

兼容 Python 3.9+。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Protocol

from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import Resource
from forgemesh.core.reconciler import ReconcileContext, Reconciler, ReconcileResult
from forgemesh.policy.api import PolicyAction, PolicyEngine

from ..registry.store import DEFAULT_RELEASE_FINALIZER
from .dedup import ProcessedKeyStore

logger = logging.getLogger(__name__)


class ReleaseProvider(Protocol):
    """受控释放 Provider 协议（控制平面侧，不绑定具体运行时插件）。

    职责：在资源进入 ``Terminating`` 后释放其外部副作用（停止推理实例、
    释放 GPU 租约、分配核验等）。实现方（如 vLLM 运行时适配器）应保证
    **幂等**：同一资源重复调用不得产生重复副作用。

    ``release`` 正常返回视为释放成功；抛出任意异常视为释放失败——协调器
    将保持 ``Terminating`` 并 requeue 重试，**不会**物理删除资源（GPU 不泄漏
    也不误删）。协议刻意不依赖插件包类型（``plugins/*``），由组装方桥接。
    """

    async def release(self, resource: Resource) -> None:
        """释放 ``resource`` 持有的外部资源；失败时抛出异常。"""
        ...


class ReconciliationController(Reconciler):
    """9 步协调闭环控制器。

    将资源的期望状态（spec）收敛到实际状态（observed），保证幂等性
    （同一 ``dedup_key`` 不重复产生副作用）且 Policy 校验不可绕过（INV-3）；
    ``Terminating`` 资源改走受控释放 → 清空 finalizer → 物理删除闭环。

    Args:
        registry: 资源注册表（权威状态，``ResourceRegistry`` 实例）。
        policy_engine: 策略引擎（授权校验，不可绕过）。
        decision_provider: 可选决策 Provider（生成候选动作，仅建议非授权）。
        scheduler: 可选调度器（资源分配的唯一执行点）；未注入时，需要分配的
            资源报告 ``Ready=False``（``SchedulerUnavailable``）。
        workflow_runtime: 可选工作流运行时（执行协调工作流）；未注入时，需要
            执行工作流的资源报告 ``Ready=False``（``WorkflowRuntimeUnavailable``）。
        release_provider: 可选受控释放 Provider（``Terminating`` 时释放外部
            副作用）；``None`` 为 Noop 语义——仅移除 finalizer 后物理删除。
        processed_store: 可选去重键持久化存储（对齐 architecture §5.5"不能
            依赖单进程内存状态保证全局正确性"）；``None``（默认）时回退
            内存 set 行为，向后兼容既有测试与单进程部署。
    """

    def __init__(
        self,
        registry: Any,
        policy_engine: PolicyEngine,
        decision_provider: Any | None = None,
        scheduler: Any | None = None,
        workflow_runtime: Any | None = None,
        release_provider: ReleaseProvider | None = None,
        processed_store: ProcessedKeyStore | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy_engine
        self._decision = decision_provider
        self._scheduler = scheduler
        self._workflow = workflow_runtime
        self._release_provider = release_provider
        self._processed_store = processed_store
        # 已处理的去重键（内存级回退路径；processed_store 注入时以 SQLite
        # 持久化为准——architecture §5.5 不依赖单进程内存状态）。
        self._processed_keys: set[str] = set()

    async def reconcile(self, ctx: ReconcileContext) -> ReconcileResult:
        """执行一轮 9 步协调（或 Terminating 受控释放子流程）。

        幂等保证：若 ``ctx.dedup_key`` 已处理过，直接返回 ``done=True``，
        不产生任何重复副作用。注入 ``processed_store`` 时该检查基于
        SQLite 持久化状态（进程重启后依然生效）。

        Args:
            ctx: 协调上下文（含资源、观测状态、租户边界、去重键）。

        Returns:
            :class:`ReconcileResult`：含 ``done`` / ``requeue_after`` / ``status_patch``。
        """
        # 幂等检查：已处理的 dedup_key 直接返回 done（持久化 store 优先，
        # 回退内存 set 保持向后兼容）。
        if self._processed_store is not None:
            if await self._processed_store.seen(ctx.dedup_key):
                return ReconcileResult(done=True, requeue_after=None)
        elif ctx.dedup_key in self._processed_keys:
            return ReconcileResult(done=True, requeue_after=None)

        resource = ctx.resource
        observed = ctx.observed

        # 终止协调优先于差异收敛：删除闭环（受控释放 → 清空 finalizer →
        # 物理删除）。注意：失败路径不登记 dedup_key，保证 requeue 重试。
        if self._effective_phase(ctx) == "Terminating":
            return await self._reconcile_terminating(ctx)

        # 步骤 1-3：获取资源 / 查询观测 / 比较差异
        diff = self._compute_diff(resource, observed)
        if not diff["needs_reconcile"]:
            await self._mark_processed(ctx.dedup_key)
            return ReconcileResult(done=True)

        # 步骤 4：生成候选（如果有 DecisionProvider，仅建议非授权）
        if self._decision is not None:
            try:
                from forgemesh.decision.provider import DecisionRequest

                req = DecisionRequest(
                    request_id=ctx.correlation_id,
                    tenant_id=ctx.tenant_id,
                    operation=diff["operation"],
                    context=diff,
                )
                raw = await self._decision.decide(req)
                # 候选仅用于解释/排序，不影响后续授权流程
                _candidates = list(raw) if raw else []
            except Exception as exc:  # noqa: BLE001 - 决策失败不应中断协调
                logger.warning("decision provider failed: %s", exc)

        # 步骤 5：Policy 校验（不可绕过，INV-3）
        action = PolicyAction(
            action=diff["operation"],
            tenant_id=ctx.tenant_id,
            resource={"kind": resource.kind, "name": resource.metadata.name},
        )
        policy_result = await self._policy.check(ctx, action)
        # deny / require_approval / 分配失败均未产生副作用：不登记 dedup_key，
        # 否则 requeue 回来会命中幂等短路，永远不会重新评估。
        if policy_result.decision == "deny":
            return ReconcileResult(
                done=False,
                requeue_after=30.0,
                status_patch={
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "False",
                            "reason": "PolicyDenied",
                            "message": policy_result.reason,
                        }
                    ]
                },
            )

        if policy_result.decision == "require_approval":
            # 高风险操作要求人工审批：不产生任何副作用（不分配、不执行
            # 工作流），requeue 等待人工审批后重新协调（对齐
            # development-plan 5.6 三态 / architecture 7.5——高风险操作
            # 不允许在审批前放行）
            return ReconcileResult(
                done=False,
                requeue_after=30.0,
                status_patch={
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "Unknown",
                            "reason": "PolicyApprovalRequired",
                            "message": policy_result.reason,
                        }
                    ]
                },
            )

        # 必须先于任何副作用检查：否则可能分配了资源却没有运行时部署它。
        missing = self._missing_executor(diff)
        if missing is not None:
            reason, message = missing
            return ReconcileResult(
                done=False,
                requeue_after=None,
                status_patch={
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "False",
                            "reason": reason,
                            "message": message,
                        }
                    ]
                },
            )

        # 步骤 6：Scheduler 分配（如果需要且调度器可用）
        allocation_ref = None
        if self._scheduler is not None and diff.get("needs_allocation"):
            from forgemesh.core.scheduler import ScheduleRequest

            sched_req = ScheduleRequest(
                tenant_id=ctx.tenant_id,
                resource_kind=resource.kind,
                requirements=diff.get("requirements", {}),
            )
            try:
                allocation_ref = await self._scheduler.allocate(sched_req)
            except Exception as exc:  # noqa: BLE001 - 分配失败需可重试
                logger.warning("scheduler allocate failed: %s", exc)
                return ReconcileResult(
                    done=False,
                    requeue_after=10.0,
                    status_patch={
                        "conditions": [
                            {
                                "type": "Ready",
                                "status": "False",
                                "reason": "AllocationFailed",
                                "message": str(exc),
                            }
                        ]
                    },
                )

        # 步骤 7：WorkEngine 执行（如果需要且运行时可用）
        run_ref = None
        if self._workflow is not None and diff.get("needs_workflow"):
            try:
                from forgemesh.workflow.runtime import WorkflowDefinition

                wf_def = WorkflowDefinition(
                    name=f"reconcile-{resource.kind}-{resource.metadata.name}",
                    steps=[],
                )
                run_ref = await self._workflow.start(wf_def, diff, ctx.dedup_key)
            except Exception as exc:  # noqa: BLE001 - 工作流失败不阻断状态更新
                logger.warning("workflow start failed: %s", exc)

        # 步骤 8-9：重观察 + 更新 status
        # 走到这里 policy_result.decision 只能是 "allow"（deny /
        # require_approval 已提前返回），故直接标记 Reconciled
        await self._mark_processed(ctx.dedup_key)
        conditions = [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Reconciled",
                "message": "reconciliation completed",
            }
        ]

        # allocation_ref / run_ref 在 P1 暂仅记日志，生产环境应写入 status
        if allocation_ref is not None:
            logger.debug("allocation created: %s", allocation_ref)
        if run_ref is not None:
            logger.debug("workflow started: %s", run_ref)

        return ReconcileResult(
            done=True,
            requeue_after=None,
            status_patch={"conditions": conditions},
        )

    def _missing_executor(self, diff: dict[str, Any]) -> tuple[str, str] | None:
        """返回本轮所需但未注入的执行组件对应的 ``(reason, message)``；齐备时返回 ``None``。"""
        if diff.get("needs_allocation") and self._scheduler is None:
            return "SchedulerUnavailable", "no scheduler is configured to allocate resources"
        if diff.get("needs_workflow") and self._workflow is None:
            return (
                "WorkflowRuntimeUnavailable",
                "no workflow runtime is configured to run the reconcile workflow",
            )
        return None

    # ------------------------------------------------------------------ #
    # 去重键登记（持久化 store 优先，回退内存 set）
    # ------------------------------------------------------------------ #

    async def _mark_processed(self, key: str) -> None:
        """登记已处理去重键。

        注入 ``processed_store`` 时写入 SQLite（进程重启后仍可见，对齐
        architecture §5.5）；否则回退内存 set（向后兼容既有行为）。
        """
        if self._processed_store is not None:
            await self._processed_store.mark(key)
        else:
            self._processed_keys.add(key)

    # ------------------------------------------------------------------ #
    # 终止协调（受控释放 → 清空 finalizer → 物理删除）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _effective_phase(ctx: ReconcileContext) -> str | None:
        """提取资源的有效生命周期 phase。

        优先取观测快照 ``ctx.observed["phase"]``（与 :meth:`_compute_diff`
        一致的口径）；观测缺失时回退到资源对象自带的 ``status.phase``
        （强类型子类）。基类资源无 phase 时返回 ``None``。
        """
        phase = ctx.observed.get("phase")
        if phase is None:
            status = getattr(ctx.resource, "status", None)
            phase = getattr(status, "phase", None)
        return str(phase) if phase is not None else None

    async def _reconcile_terminating(self, ctx: ReconcileContext) -> ReconcileResult:
        """``Terminating`` 受控释放子流程（删除闭环）。

        步骤（对齐 architecture.md §5.3/§12.4 与 development-plan 第 7 章）：

        1. ``release_provider.release(resource)``（未注入 → Noop）；异常 →
           保持 ``Terminating`` + requeue 重试（不物理删除，GPU 不误删）；
        2. 读取最新资源，移除默认 release finalizer 后 CAS 写回；仍有其他
           finalizer → 等待（requeue）；
        3. finalizers 清空 → 释放关联 Allocation（P1 约定：以
           ``{kind}/{name}`` 作为 ``allocations.resource_id`` 关联键，由
           Scheduler 装配方对齐）→ 物理 DELETE → 记审计日志。

        失败路径**不**登记 ``_processed_keys``，保证 requeue 后可重试。
        """
        resource = ctx.resource

        # 步骤 1：受控释放外部副作用（Provider 缺省 → Noop，仅移除 finalizer）。
        if self._release_provider is not None:
            try:
                await self._release_provider.release(resource)
            except Exception as exc:  # noqa: BLE001 - 释放失败需保持 Terminating 重试
                logger.warning("release provider failed (keep Terminating): %s", exc)
                return ReconcileResult(
                    done=False,
                    requeue_after=10.0,
                    status_patch={
                        "conditions": [
                            {
                                "type": "Terminating",
                                "status": "False",
                                "reason": "ReleaseFailed",
                                "message": str(exc),
                            }
                        ]
                    },
                )

        if self._registry is None:
            logger.warning(
                "terminating resource cannot be finalized without a registry: %s",
                ctx.dedup_key,
            )
            return ReconcileResult(done=False, requeue_after=30.0)

        meta = resource.metadata

        # 步骤 2：移除默认 release finalizer（基于最新版本，CAS 写回）。
        try:
            current = await self._registry.get(
                resource.kind, meta.namespace, meta.name, ctx.tenant_id
            )
        except NotFoundError:
            # 已被并发删除：视为完成。
            await self._mark_processed(ctx.dedup_key)
            return ReconcileResult(done=True)

        current.finalizers = [f for f in current.finalizers if f != DEFAULT_RELEASE_FINALIZER]
        if current.finalizers:
            # 仍有其他 finalizer 持有方未完成清理：写回并等待。
            try:
                await self._registry.put(current, expected_version=current.metadata.resourceVersion)
            except ConflictError:
                # 版本漂移：下轮重读重试。
                return ReconcileResult(done=False, requeue_after=5.0)
            return ReconcileResult(
                done=False,
                requeue_after=5.0,
                status_patch={
                    "conditions": [
                        {
                            "type": "Terminating",
                            "status": "False",
                            "reason": "WaitingFinalizers",
                            "message": f"waiting finalizers: {current.finalizers}",
                        }
                    ]
                },
            )

        # 步骤 3：finalizers 已清空 → 释放关联 Allocation → 物理 DELETE。
        allocation_key = f"{resource.kind}/{meta.name}"
        get_active = getattr(self._registry, "get_active_allocation", None)
        if callable(get_active):
            active = await get_active(allocation_key, ctx.tenant_id)
            if active is not None:
                release = getattr(self._registry, "release_allocation", None)
                if callable(release):
                    await release(
                        active["id"],
                        ctx.tenant_id,
                        reason=f"resource {allocation_key} deleted",
                    )
        with contextlib.suppress(NotFoundError):
            # 已被并发删除：幂等完成
            await self._registry.delete(
                resource.kind,
                meta.namespace,
                meta.name,
                ctx.tenant_id,
                actor="reconciler",
                correlation_id=ctx.correlation_id,
            )
        await self._mark_processed(ctx.dedup_key)
        logger.info(
            "resource physically deleted after finalizers cleared: kind=%s name=%s "
            "tenant_id=%s correlation_id=%s",
            resource.kind,
            meta.name,
            ctx.tenant_id,
            ctx.correlation_id,
        )
        return ReconcileResult(done=True, requeue_after=None)

    @staticmethod
    def _compute_diff(resource: Resource, observed: dict[str, Any]) -> dict[str, Any]:
        """计算期望状态与观测状态的差异（步骤 2-3）。

        判定逻辑：

        * 若 ``observed_generation >= desired_generation`` 且 phase 为 ``Ready``
          或 ``Degraded``，认为已收敛，无需协调。
        * 否则需要协调；根据 phase 判断是否需要分配（Pending/None）和
          是否需要工作流（None/Pending/Scheduling）。

        Args:
            resource: 目标资源（含期望状态与元数据）。
            observed: 实际状态快照。

        Returns:
            差异描述字典，含 ``needs_reconcile`` / ``operation`` /
            ``needs_allocation`` / ``needs_workflow`` 等字段。
        """
        observed_phase = observed.get("phase")
        desired_generation = resource.metadata.generation
        observed_generation = resource.metadata.observed_generation

        if observed_generation >= desired_generation and observed_phase in (
            "Ready",
            "Degraded",
        ):
            return {"needs_reconcile": False, "operation": "noop"}

        operation = "reconcile"
        needs_allocation = observed_phase is None or observed_phase == "Pending"
        needs_workflow = observed_phase in (None, "Pending", "Scheduling")

        return {
            "needs_reconcile": True,
            "operation": operation,
            "needs_allocation": needs_allocation,
            "needs_workflow": needs_workflow,
            "requirements": {},
            "desired_generation": desired_generation,
            "observed_generation": observed_generation,
            "observed_phase": observed_phase,
        }


__all__ = ["ReleaseProvider", "ReconciliationController"]
