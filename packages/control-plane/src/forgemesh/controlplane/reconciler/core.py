"""按 phase 推进的协调闭环。

对齐 development-plan 5.3 / architecture 6.2。协调器将资源的期望状态（spec）收敛到实际
状态：每一轮只做一个合法的 phase 转移，写回 status 后由 ``requeue_after`` 安排下一轮
（status 写入不会触发协调）。任何一轮都可能在 status 写回之前中断并重放，所以每个副作用
都按幂等键执行（INV-1）；Policy 校验不可绕过（INV-3）。

部署 ``InferenceService``
-------------------------
``Pending → Scheduling``
    DecisionProvider 生成候选（建议，非授权），PolicyEngine.check() 授权这次部署；deny /
    require_approval 时停在 ``Pending``。策略只在这里评估，之后各轮执行已授权的部署。
``Scheduling → Preparing``
    Scheduler.allocate()（唯一授权分配点）。请求携带资源身份（kind/namespace/name/uid）、
    幂等键（uid + generation，见
    :func:`~forgemesh.controlplane.reconciler.dedup.allocation_idempotency_key`）与取自
    ``spec.resources.accelerator`` 的调度需求：重放得到同一分配。
``Preparing → Deploying``
    以同一幂等键再次 allocate() 取回分配，然后 WorkflowRuntime.start() 启动部署工作流
    （:mod:`forgemesh.controlplane.deployment.workflow`），幂等键为
    :func:`~forgemesh.controlplane.deployment.workflow.deployment_run_key`（uid），输入是
    携带分配设备的 :class:`~forgemesh.controlplane.deployment.launcher.LaunchRequest`。
``Deploying → Verifying → Ready``
    按同一幂等键 find_run() 查询运行：实例启动后进入 ``Verifying``；运行成功后进入
    ``Ready``，写入 ``status.endpoints``（取自 ``launch-instance`` 的结果）与 ``Ready=True``。
    运行未结束时每 ``workflow_poll_interval`` 秒查询一次。

只有进入 ``Ready`` 或 ``Failed`` 的一轮返回 ``done=True``（登记去重键）；其余各轮返回
``done=False``，同一 generation 的去重键不会挡住下一轮。

失败
----
* 分配失败：停在当前 phase，``Ready=False``（``AllocationFailed``），10 秒后重试。
* 部署工作流没能启动（运行没能创建或启动）：停在 ``Preparing``，``Ready=False``
  （``WorkflowStartFailed``），保留分配，按指数退避重试；查询运行失败同样退避重试
  （``WorkflowLookupFailed``）。
* 部署运行失败（某一步用尽重试，工作流已补偿、停止了实例）、被取消或超时：
  ``phase=Failed``、``Ready=False``（``WorkflowFailed``，消息含失败的步骤），释放分配。
  补偿没能停止实例时保留分配，删除资源时再停止实例、释放分配；找不到运行
  （``WorkflowRunNotFound``，如工作流数据库被重置）或运行成功却没有报告实例（运行时
  不返回步骤输出）时同样进入 ``Failed`` 并保留分配。
* ``Failed`` 是终态：资源状态机只允许它转入 ``Terminating``，协调器不自动重试，删除后
  重新创建资源即可再次部署。

需要分配或执行工作流、却未注入对应组件时，在任何副作用之前返回 ``Ready=False``
（``SchedulerUnavailable`` / ``WorkflowRuntimeUnavailable``）。没有 ``metadata.uid`` 的
资源报告 ``MissingUid``，不是 ``InferenceService`` 的资源报告 ``NotDeployable``。处于
``Ready`` / ``Degraded`` / ``Updating`` / ``Failed`` / ``Unknown`` 的资源没有可做的事：还
没有更新已部署服务的流程，``spec`` 变化（新 generation）不会重新部署。

终止协调（Terminating，对齐 architecture.md §5.3/§12.4 与 development-plan
第 7 章删除闭环）
--------------------------------------------------------------------
观测 ``phase == Terminating`` 时不走部署推进，改走受控释放子流程：

1. 调用注入的 :class:`ReleaseProvider` 释放外部副作用（如
   :class:`~forgemesh.controlplane.deployment.DeploymentReleaser` 取消进行中的部署并
   停止实例；未注入时为 Noop 语义，仅移除 finalizer）；失败 → 保持 ``Terminating`` 并
   requeue 重试；
2. 移除默认 release finalizer（``forgemesh.io/resource-release``）并 CAS 写回；
3. finalizers 全部清空后：释放归属该资源的 Active Allocation 及其设备（按
   ``metadata.uid`` 查找，限本租户）→ 物理 DELETE → 记审计日志。资源已被删除、
   或同名资源已以新 uid 重建时，只释放原资源的分配，不触碰新资源。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Protocol

from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import Resource
from forgemesh.core.reconciler import ReconcileContext, Reconciler, ReconcileResult
from forgemesh.core.scheduler import AllocationRef, ResourceRef, ScheduleRequest
from forgemesh.policy.api import PolicyAction, PolicyEngine
from forgemesh.resource.inference import AcceleratorSpec, InferencePhase, InferenceService
from forgemesh.workflow.runtime import RunInfo
from forgemesh.workflow.states import StepStatus, WorkflowStatus

from ..deployment import (
    LAUNCH_INSTANCE,
    LaunchedInstance,
    LaunchRequest,
    deployment_inputs,
    deployment_run_key,
    deployment_workflow,
    launched_instance,
)
from ..registry.store import DEFAULT_RELEASE_FINALIZER
from .dedup import ProcessedKeyStore, allocation_idempotency_key

logger = logging.getLogger(__name__)

#: 启动或查询部署运行失败后重试间隔的上限（秒）。
MAX_RETRY_DELAY_SECONDS = 60.0

#: 部署过程中的 phase（``None`` 为尚未观测到 phase）；其余 phase 没有可做的事。
_DEPLOYMENT_PHASES: frozenset[str | None] = frozenset(
    {
        None,
        InferencePhase.PENDING,
        InferencePhase.SCHEDULING,
        InferencePhase.PREPARING,
        InferencePhase.DEPLOYING,
        InferencePhase.VERIFYING,
    }
)
#: 本轮或之后的轮次还要调用调度器的 phase（``Preparing`` 以同一幂等键取回分配）。
_ALLOCATION_PHASES: frozenset[str | None] = frozenset(
    {None, InferencePhase.PENDING, InferencePhase.SCHEDULING, InferencePhase.PREPARING}
)


def _scheduling_requirements(resource: Resource) -> dict[str, Any]:
    """提取资源的调度需求 ``{"accelerator": {"type", "count", "minMemoryGiB"}}``。

    取自强类型 spec 的 ``spec.resources.accelerator``；没有加速器申请的资源（基类
    ``Resource``、未登记的 kind）返回 ``{}``。
    """
    spec = getattr(resource, "spec", None)
    accelerator = getattr(getattr(spec, "resources", None), "accelerator", None)
    if not isinstance(accelerator, AcceleratorSpec):
        return {}
    return {"accelerator": accelerator.model_dump(mode="json")}


def _observed_phase(resource: Resource, observed: dict[str, Any]) -> str | None:
    """资源的生命周期 phase。

    优先取观测快照 ``observed["phase"]``；观测缺失时回退到资源对象自带的
    ``status.phase``（强类型子类）。基类资源无 phase 时返回 ``None``。
    """
    phase = observed.get("phase")
    if phase is None:
        status = getattr(resource, "status", None)
        phase = getattr(status, "phase", None)
    return str(phase) if phase is not None else None


def _resource_ref(resource: Resource) -> ResourceRef:
    meta = resource.metadata
    return ResourceRef(kind=resource.kind, namespace=meta.namespace, name=meta.name, uid=meta.uid)


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _ready_condition(status: str, reason: str, message: str) -> dict[str, str]:
    return {"type": "Ready", "status": status, "reason": reason, "message": message}


def _not_ready(reason: str, message: str, requeue_after: float | None) -> ReconcileResult:
    """停在当前 phase：``Ready=False`` 并说明原因，不登记去重键。"""
    return ReconcileResult(
        done=False,
        requeue_after=requeue_after,
        status_patch={"conditions": [_ready_condition("False", reason, message)]},
    )


def _progress(phase: InferencePhase, message: str, requeue_after: float) -> ReconcileResult:
    """进入部署过程中的下一个 phase（``Ready=False``，原因即该 phase），不登记去重键。"""
    return ReconcileResult(
        done=False,
        requeue_after=requeue_after,
        status_patch={
            "phase": phase.value,
            "conditions": [_ready_condition("False", phase.value, message)],
        },
    )


class ReleaseProvider(Protocol):
    """受控释放 Provider 协议（控制平面侧，不绑定具体运行时插件）。

    职责：在资源进入 ``Terminating`` 后释放其外部副作用（取消进行中的部署、停止推理
    实例等，如 :class:`~forgemesh.controlplane.deployment.DeploymentReleaser`）。实现方
    应保证**幂等**：同一资源重复调用不得产生重复副作用。

    ``release`` 正常返回视为释放成功；抛出任意异常视为释放失败——协调器
    将保持 ``Terminating`` 并 requeue 重试，**不会**物理删除资源（GPU 不泄漏
    也不误删）。协议刻意不依赖插件包类型（``plugins/*``），由组装方桥接。
    """

    async def release(self, resource: Resource) -> None:
        """释放 ``resource`` 持有的外部资源；失败时抛出异常。"""
        ...


class ReconciliationController(Reconciler):
    """按 phase 推进的协调控制器（流程见模块说明）。

    将资源的期望状态（spec）收敛到实际状态（observed），保证幂等性
    （同一 ``dedup_key`` 不重复产生副作用）且 Policy 校验不可绕过（INV-3）；
    ``Terminating`` 资源改走受控释放 → 清空 finalizer → 物理删除闭环。

    Args:
        registry: 资源注册表（权威状态，``ResourceRegistry`` 实例）。
        policy_engine: 策略引擎（授权校验，不可绕过）。
        decision_provider: 可选决策 Provider（生成候选动作，仅建议非授权）。
        scheduler: 可选调度器（资源分配的唯一执行点）；未注入时，需要分配的
            资源报告 ``Ready=False``（``SchedulerUnavailable``）。
        workflow_runtime: 可选工作流运行时（执行部署工作流，须实现 ``find_run``，返回
            的快照须带步骤输出）；未注入时，需要部署的资源报告 ``Ready=False``
            （``WorkflowRuntimeUnavailable``）。
        release_provider: 受控释放 Provider（``Terminating`` 时释放外部副作用）。注入
            工作流运行时时必须同时注入（如
            :class:`~forgemesh.controlplane.deployment.DeploymentReleaser`），否则删除资源
            会留下运行中的实例；只在没有工作流运行时时可以为 ``None``（Noop 语义——仅
            移除 finalizer 后物理删除）。
        processed_store: 可选去重键持久化存储（对齐 architecture §5.5"不能
            依赖单进程内存状态保证全局正确性"）；``None``（默认）时回退
            内存 set 行为，向后兼容既有测试与单进程部署。
        record_dedup_keys: 为 ``False`` 时 :meth:`reconcile` 只检查、不登记去重键，
            由调用方在持久化 ``status_patch`` 之后调用 :meth:`mark_processed`，
            避免"已登记、状态却没写进去"（控制器管理器使用）。默认 ``True``。
        workflow_poll_interval: 部署运行进行中时两次查询之间的秒数，也是启动或查询失败
            后退避的起点（之后每次翻倍，最多 :data:`MAX_RETRY_DELAY_SECONDS`）。

    Raises:
        ValueError: 注入了工作流运行时却没有 ``release_provider``，或
            ``workflow_poll_interval`` 不是正数。
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
        record_dedup_keys: bool = True,
        workflow_poll_interval: float = 2.0,
    ) -> None:
        if workflow_runtime is not None and release_provider is None:
            raise ValueError(
                "a release provider is required together with a workflow runtime; without "
                "one, deleting a resource would leave its instance running"
            )
        if workflow_poll_interval <= 0:
            raise ValueError("workflow_poll_interval must be > 0")
        self._registry = registry
        self._policy = policy_engine
        self._decision = decision_provider
        self._scheduler = scheduler
        self._workflow = workflow_runtime
        self._release_provider = release_provider
        self._processed_store = processed_store
        self._record_dedup_keys = record_dedup_keys
        self._poll_interval = workflow_poll_interval
        # 已处理的去重键（内存级回退路径；processed_store 注入时以 SQLite
        # 持久化为准——architecture §5.5 不依赖单进程内存状态）。
        self._processed_keys: set[str] = set()
        # 按资源 uid 记录连续的启动 / 查询失败次数，只用于计算退避。
        self._retry_counts: dict[str, int] = {}

    @property
    def records_dedup_keys(self) -> bool:
        """:meth:`reconcile` 是否自行登记去重键。"""
        return self._record_dedup_keys

    async def reconcile(self, ctx: ReconcileContext) -> ReconcileResult:
        """执行一轮协调：推进一个 phase，或执行 Terminating 受控释放子流程。

        幂等保证：若 ``ctx.dedup_key`` 已处理过，直接返回 ``done=True``，
        不产生任何重复副作用。注入 ``processed_store`` 时该检查基于
        SQLite 持久化状态（进程重启后依然生效）。``record_dedup_keys=False``
        时本方法不登记去重键，由调用方调用 :meth:`mark_processed`。

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
        phase = _observed_phase(resource, ctx.observed)

        # 终止协调优先：删除闭环（受控释放 → 清空 finalizer → 物理删除）。
        # 注意：失败路径不登记 dedup_key，保证 requeue 重试。
        if phase == InferencePhase.TERMINATING:
            return await self._reconcile_terminating(ctx)

        diff = self._compute_diff(resource, ctx.observed)
        if not diff["needs_reconcile"]:
            await self._mark_processed(ctx.dedup_key)
            return ReconcileResult(done=True)

        if phase in (None, InferencePhase.PENDING):
            blocked = await self._authorize(ctx, diff)
            if blocked is not None:
                return blocked

        # 必须先于任何副作用检查：否则可能分配了资源却没有运行时部署它。
        missing = self._missing_executor(diff)
        if missing is not None:
            reason, message = missing
            return _not_ready(reason, message, requeue_after=None)
        if not resource.metadata.uid:
            return _not_ready(
                "MissingUid", "metadata.uid is required to allocate resources", requeue_after=None
            )
        if not isinstance(resource, InferenceService):
            return _not_ready(
                "NotDeployable",
                f"{resource.kind} resources have no deployment spec",
                requeue_after=None,
            )

        if phase in (None, InferencePhase.PENDING):
            return _progress(
                InferencePhase.SCHEDULING, "allocating accelerators", requeue_after=0.0
            )
        if phase == InferencePhase.SCHEDULING:
            return await self._schedule(ctx, resource)
        if phase == InferencePhase.PREPARING:
            return await self._start_deployment(ctx, resource)
        return await self._follow_deployment(ctx, resource, phase)

    async def _authorize(
        self, ctx: ReconcileContext, diff: dict[str, Any]
    ) -> ReconcileResult | None:
        """生成候选并做 Policy 校验；放行时返回 ``None``，否则返回停在 ``Pending`` 的结果。"""
        resource = ctx.resource
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

        action = PolicyAction(
            action=diff["operation"],
            tenant_id=ctx.tenant_id,
            resource={"kind": resource.kind, "name": resource.metadata.name},
        )
        policy_result = await self._policy.check(ctx, action)
        # deny / require_approval 均未产生副作用：不登记 dedup_key，
        # 否则 requeue 回来会命中幂等短路，永远不会重新评估。
        if policy_result.decision == "deny":
            return _not_ready("PolicyDenied", policy_result.reason, requeue_after=30.0)
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
                        _ready_condition("Unknown", "PolicyApprovalRequired", policy_result.reason)
                    ]
                },
            )
        return None

    def _missing_executor(self, diff: dict[str, Any]) -> tuple[str, str] | None:
        """返回本轮所需但未注入的执行组件对应的 ``(reason, message)``；齐备时返回 ``None``。"""
        if diff.get("needs_allocation") and self._scheduler is None:
            return "SchedulerUnavailable", "no scheduler is configured to allocate resources"
        if diff.get("needs_workflow") and self._workflow is None:
            return (
                "WorkflowRuntimeUnavailable",
                "no workflow runtime is configured to run the deployment workflow",
            )
        return None

    # ------------------------------------------------------------------ #
    # 部署推进（Scheduling → Preparing → Deploying → Verifying → Ready）
    # ------------------------------------------------------------------ #

    async def _allocate(self, ctx: ReconcileContext, resource: Resource) -> AllocationRef:
        meta = resource.metadata
        request = ScheduleRequest(
            tenant_id=ctx.tenant_id,
            resource_ref=_resource_ref(resource),
            idempotency_key=allocation_idempotency_key(meta.uid, meta.generation),
            requirements=_scheduling_requirements(resource),
        )
        return await self._scheduler.allocate(request)

    @staticmethod
    def _allocation_failed(exc: Exception) -> ReconcileResult:
        logger.warning("scheduler allocate failed: %s", exc)
        return _not_ready("AllocationFailed", _describe(exc), requeue_after=10.0)

    async def _schedule(self, ctx: ReconcileContext, service: InferenceService) -> ReconcileResult:
        """``Scheduling`` 一轮：分配加速器后进入 ``Preparing``。"""
        try:
            allocation = await self._allocate(ctx, service)
        except Exception as exc:  # noqa: BLE001 - 分配失败需可重试
            return self._allocation_failed(exc)
        devices = ", ".join(f"{d.node_id}/{d.device_id}" for d in allocation.devices)
        return _progress(
            InferencePhase.PREPARING,
            f"allocation {allocation.allocation_id} holds {devices or 'no devices'}",
            requeue_after=0.0,
        )

    async def _start_deployment(
        self, ctx: ReconcileContext, service: InferenceService
    ) -> ReconcileResult:
        """``Preparing`` 一轮：取回分配，启动部署工作流后进入 ``Deploying``。"""
        try:
            allocation = await self._allocate(ctx, service)
        except Exception as exc:  # noqa: BLE001 - 分配失败需可重试
            return self._allocation_failed(exc)
        uid = service.metadata.uid
        run_key = deployment_run_key(uid)
        request = LaunchRequest(
            instance_key=run_key,
            tenant_id=ctx.tenant_id,
            resource=_resource_ref(service),
            spec=service.spec,
            allocation_id=allocation.allocation_id,
            devices=tuple(allocation.devices),
        )
        try:
            run = await self._workflow.start(
                deployment_workflow(), deployment_inputs(request), run_key
            )
        except Exception as exc:  # noqa: BLE001 - 启动失败：保留分配，退避重试
            delay = self._next_retry_delay(uid)
            logger.warning(
                "deployment workflow start failed (retrying in %.1fs): %s", delay, _describe(exc)
            )
            return _not_ready("WorkflowStartFailed", _describe(exc), requeue_after=delay)
        self._retry_counts.pop(uid, None)
        return _progress(
            InferencePhase.DEPLOYING,
            f"deployment workflow run {run.run_id} started",
            requeue_after=self._poll_interval,
        )

    async def _follow_deployment(
        self, ctx: ReconcileContext, service: InferenceService, phase: str
    ) -> ReconcileResult:
        """``Deploying`` / ``Verifying`` 一轮：按幂等键查询部署运行并推进。"""
        uid = service.metadata.uid
        run_key = deployment_run_key(uid)
        try:
            run = await self._workflow.find_run(run_key)
        except Exception as exc:  # noqa: BLE001 - 查询失败：保持当前 phase，退避重试
            delay = self._next_retry_delay(uid)
            logger.warning(
                "deployment workflow lookup failed (retrying in %.1fs): %s",
                delay,
                _describe(exc),
            )
            return _not_ready("WorkflowLookupFailed", _describe(exc), requeue_after=delay)
        self._retry_counts.pop(uid, None)
        if run is None:
            # 运行记录丢失：无法确认是否有实例在运行，分配留到删除资源时释放。
            return await self._fail(
                ctx,
                "WorkflowRunNotFound",
                f"deployment workflow run {run_key!r} not found",
                release_allocation=False,
            )
        instance = launched_instance(run)
        if run.status is WorkflowStatus.SUCCEEDED and instance is not None:
            if phase == InferencePhase.DEPLOYING:
                return self._verifying(instance, requeue_after=0.0)
            return await self._ready(ctx, instance)
        if run.terminal:
            return await self._fail_run(ctx, run, instance)
        if (
            phase == InferencePhase.DEPLOYING
            and instance is not None
            and run.steps.get(LAUNCH_INSTANCE) is StepStatus.SUCCEEDED
            and run.status is not WorkflowStatus.COMPENSATING
        ):
            return self._verifying(instance, requeue_after=self._poll_interval)
        return ReconcileResult(done=False, requeue_after=self._poll_interval)

    @staticmethod
    def _verifying(instance: LaunchedInstance, requeue_after: float) -> ReconcileResult:
        return _progress(
            InferencePhase.VERIFYING,
            f"instance {instance.instance_id} launched at {instance.url}",
            requeue_after=requeue_after,
        )

    async def _ready(self, ctx: ReconcileContext, instance: LaunchedInstance) -> ReconcileResult:
        """运行成功：进入 ``Ready``，写入实例地址。"""
        await self._mark_processed(ctx.dedup_key)
        meta = ctx.resource.metadata
        logger.info(
            "deployment ready: instance_id=%s url=%s kind=%s namespace=%s name=%s uid=%s "
            "tenant_id=%s correlation_id=%s",
            instance.instance_id,
            instance.url,
            ctx.resource.kind,
            meta.namespace,
            meta.name,
            meta.uid,
            ctx.tenant_id,
            ctx.correlation_id,
        )
        return ReconcileResult(
            done=True,
            requeue_after=None,
            status_patch={
                "phase": InferencePhase.READY.value,
                "endpoints": [instance.endpoint().model_dump(mode="json")],
                "conditions": [
                    _ready_condition(
                        "True",
                        "Reconciled",
                        f"instance {instance.instance_id} is serving at {instance.url}",
                    )
                ],
            },
        )

    async def _fail_run(
        self, ctx: ReconcileContext, run: RunInfo, instance: LaunchedInstance | None
    ) -> ReconcileResult:
        """运行以失败告终（或成功却没有报告实例）：进入 ``Failed``。"""
        if run.status is WorkflowStatus.SUCCEEDED:
            # 运行时没有返回步骤输出：实例多半在运行，只是不知道它的地址。
            message = (
                f"deployment workflow run {run.run_id} succeeded without reporting an instance"
            )
            return await self._fail(ctx, "WorkflowFailed", message, release_allocation=False)
        message = run.error or f"deployment workflow run {run.run_id} ended {run.status.value}"
        if instance is not None:
            message += (
                f"; instance {instance.instance_id} may still be running and is stopped "
                "when the resource is deleted"
            )
        return await self._fail(ctx, "WorkflowFailed", message, release_allocation=instance is None)

    async def _fail(
        self, ctx: ReconcileContext, reason: str, message: str, *, release_allocation: bool
    ) -> ReconcileResult:
        """进入终态 ``Failed``；``release_allocation`` 为真时先释放资源的分配。"""
        if release_allocation:
            await self._release_allocation(ctx, reason="deployment failed")
        await self._mark_processed(ctx.dedup_key)
        meta = ctx.resource.metadata
        logger.warning(
            "deployment failed: reason=%s message=%s kind=%s namespace=%s name=%s uid=%s "
            "tenant_id=%s correlation_id=%s",
            reason,
            message,
            ctx.resource.kind,
            meta.namespace,
            meta.name,
            meta.uid,
            ctx.tenant_id,
            ctx.correlation_id,
        )
        return ReconcileResult(
            done=True,
            requeue_after=None,
            status_patch={
                "phase": InferencePhase.FAILED.value,
                "conditions": [_ready_condition("False", reason, message)],
            },
        )

    def _next_retry_delay(self, uid: str) -> float:
        failures = self._retry_counts.get(uid, 0) + 1
        self._retry_counts[uid] = failures
        delay = self._poll_interval * 2 ** min(failures - 1, 30)
        return min(delay, MAX_RETRY_DELAY_SECONDS)

    # ------------------------------------------------------------------ #
    # 去重键登记（持久化 store 优先，回退内存 set）
    # ------------------------------------------------------------------ #

    async def mark_processed(self, key: str) -> None:
        """登记已处理去重键。

        注入 ``processed_store`` 时写入 SQLite（进程重启后仍可见，对齐
        architecture §5.5）；否则回退内存 set（向后兼容既有行为）。
        """
        if self._processed_store is not None:
            await self._processed_store.mark(key)
        else:
            self._processed_keys.add(key)

    async def _mark_processed(self, key: str) -> None:
        """:meth:`reconcile` 内部的登记点；``record_dedup_keys=False`` 时留给调用方。"""
        if self._record_dedup_keys:
            await self.mark_processed(key)

    # ------------------------------------------------------------------ #
    # 终止协调（受控释放 → 清空 finalizer → 物理删除）
    # ------------------------------------------------------------------ #

    async def _reconcile_terminating(self, ctx: ReconcileContext) -> ReconcileResult:
        """``Terminating`` 受控释放子流程（删除闭环）。

        步骤（对齐 architecture.md §5.3/§12.4 与 development-plan 第 7 章）：

        1. ``release_provider.release(resource)``（未注入 → Noop）；异常 →
           保持 ``Terminating`` + requeue 重试（不物理删除，GPU 不误删）；
        2. 读取最新资源，移除默认 release finalizer 后 CAS 写回；仍有其他
           finalizer → 等待（requeue）；资源已不存在、或 uid 已不同（同名重建）
           → 释放原资源的分配后完成；
        3. finalizers 清空 → 释放归属该资源 uid 的 Active Allocation
           （``allocations.resource_id`` 即资源 uid，限本租户）→ 物理 DELETE
           → 记审计日志。

        失败路径**不**登记 ``_processed_keys``，保证 requeue 后可重试。
        """
        resource = ctx.resource
        if resource.metadata.uid:
            self._retry_counts.pop(resource.metadata.uid, None)

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
            current = None
        if current is None or current.metadata.uid != meta.uid:
            # 已被并发删除，或同名资源已以新 uid 重建：只释放原资源的分配。
            await self._release_allocation(ctx, reason="resource deleted")
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

        # 步骤 3：finalizers 已清空 → 释放归属该资源的 Allocation → 物理 DELETE。
        await self._release_allocation(ctx, reason="resource deleted")
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

    async def _release_allocation(self, ctx: ReconcileContext, reason: str) -> None:
        """释放归属 ``ctx.resource``（按 uid，限本租户）的 Active 分配；没有则跳过。"""
        meta = ctx.resource.metadata
        release_active = getattr(self._registry, "release_active_allocation", None)
        if not meta.uid or not callable(release_active):
            return
        allocation_id = await release_active(meta.uid, ctx.tenant_id, reason=reason)
        if allocation_id is not None:
            logger.info(
                "allocation released: allocation_id=%s reason=%s kind=%s namespace=%s name=%s "
                "uid=%s tenant_id=%s correlation_id=%s",
                allocation_id,
                reason,
                ctx.resource.kind,
                meta.namespace,
                meta.name,
                meta.uid,
                ctx.tenant_id,
                ctx.correlation_id,
            )

    @staticmethod
    def _compute_diff(resource: Resource, observed: dict[str, Any]) -> dict[str, Any]:
        """计算期望状态与观测状态的差异。

        判定逻辑：

        * phase 尚未观测到（``None``）或处于部署过程中（``Pending`` 到 ``Verifying``）时
          需要协调；其余 phase 没有可做的事（``generation`` 变化也一样，还没有更新流程）。
        * ``needs_allocation``：本轮或之后的轮次要调用调度器（``Preparing`` 及之前）；
          ``needs_workflow``：部署过程中的每一轮都要用到工作流运行时；``requirements``
          为传给调度器的需求（见 :func:`_scheduling_requirements`）。

        Args:
            resource: 目标资源（含期望状态与元数据）。
            observed: 实际状态快照（缺少 ``phase`` 时取资源自带的 ``status.phase``）。

        Returns:
            差异描述字典，含 ``needs_reconcile`` / ``operation`` /
            ``needs_allocation`` / ``needs_workflow`` / ``requirements`` 等字段。
        """
        observed_phase = _observed_phase(resource, observed)
        if observed_phase not in _DEPLOYMENT_PHASES:
            return {"needs_reconcile": False, "operation": "noop"}
        return {
            "needs_reconcile": True,
            "operation": "reconcile",
            "needs_allocation": observed_phase in _ALLOCATION_PHASES,
            "needs_workflow": True,
            "requirements": _scheduling_requirements(resource),
            "desired_generation": resource.metadata.generation,
            "observed_generation": resource.metadata.observed_generation,
            "observed_phase": observed_phase,
        }


__all__ = ["MAX_RETRY_DELAY_SECONDS", "ReleaseProvider", "ReconciliationController"]
