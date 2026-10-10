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

兼容 Python 3.9+。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set

from forgemesh.core.objects import Resource
from forgemesh.core.reconciler import ReconcileContext, Reconciler, ReconcileResult
from forgemesh.policy.api import PolicyAction, PolicyEngine

logger = logging.getLogger(__name__)


class ReconciliationController(Reconciler):
    """9 步协调闭环控制器。

    将资源的期望状态（spec）收敛到实际状态（observed），保证幂等性
    （同一 ``dedup_key`` 不重复产生副作用）且 Policy 校验不可绕过（INV-3）。

    Args:
        registry: 资源注册表（权威状态，``ResourceRegistry`` 实例）。
        policy_engine: 策略引擎（授权校验，不可绕过）。
        decision_provider: 可选决策 Provider（生成候选动作，仅建议非授权）。
        scheduler: 可选调度器（资源分配的唯一执行点）。
        workflow_runtime: 可选工作流运行时（执行协调工作流）。
    """

    def __init__(
        self,
        registry: Any,
        policy_engine: PolicyEngine,
        decision_provider: Optional[Any] = None,
        scheduler: Optional[Any] = None,
        workflow_runtime: Optional[Any] = None,
    ) -> None:
        self._registry = registry
        self._policy = policy_engine
        self._decision = decision_provider
        self._scheduler = scheduler
        self._workflow = workflow_runtime
        # 已处理的去重键（内存级，P1 简化；生产环境应使用持久化存储）。
        self._processed_keys: Set[str] = set()

    async def reconcile(self, ctx: ReconcileContext) -> ReconcileResult:
        """执行一轮 9 步协调。

        幂等保证：若 ``ctx.dedup_key`` 已处理过，直接返回 ``done=True``，
        不产生任何重复副作用。

        Args:
            ctx: 协调上下文（含资源、观测状态、租户边界、去重键）。

        Returns:
            :class:`ReconcileResult`：含 ``done`` / ``requeue_after`` / ``status_patch``。
        """
        # 幂等检查：已处理的 dedup_key 直接返回 done
        if ctx.dedup_key in self._processed_keys:
            return ReconcileResult(done=True, requeue_after=None)

        resource = ctx.resource
        observed = ctx.observed

        # 步骤 1-3：获取资源 / 查询观测 / 比较差异
        diff = self._compute_diff(resource, observed)
        if not diff["needs_reconcile"]:
            self._processed_keys.add(ctx.dedup_key)
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
        if policy_result.decision == "deny":
            self._processed_keys.add(ctx.dedup_key)
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
                self._processed_keys.add(ctx.dedup_key)
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
        self._processed_keys.add(ctx.dedup_key)
        conditions = []
        if policy_result.decision == "degraded":
            conditions.append(
                {
                    "type": "Ready",
                    "status": "Unknown",
                    "reason": "PolicyDegraded",
                    "message": policy_result.reason,
                }
            )
        else:
            conditions.append(
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "Reconciled",
                    "message": "reconciliation completed",
                }
            )

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

    @staticmethod
    def _compute_diff(resource: Resource, observed: Dict[str, Any]) -> Dict[str, Any]:
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


__all__ = ["ReconciliationController"]
