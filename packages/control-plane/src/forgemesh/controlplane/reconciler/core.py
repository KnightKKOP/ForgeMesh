"""按 phase 推进的协调闭环。

对齐 development-plan 5.3 / architecture 6.2。协调器将资源的期望状态（spec）收敛到实际
状态：每一轮只做一个合法的 phase 转移，写回 status 后由 ``requeue_after`` 安排下一轮
（status 写入不会触发协调）。任何一轮都可能在 status 写回之前中断并重放，所以每个副作用
都按幂等键执行（INV-1）；Policy 校验不可绕过（INV-3）。

部署 ``InferenceService``
-------------------------
``Pending → Scheduling``
    DecisionProvider 生成候选（建议，非授权），PolicyEngine.check() 授权这次部署；deny /
    require_approval 时停在 ``Pending``。策略只在部署（这里）与更新（见下文）开始时评估，
    之后各轮执行已授权的部署。
``Scheduling → Preparing``
    Scheduler.allocate()（唯一授权分配点）。请求携带资源身份（kind/namespace/name/uid）、
    幂等键（uid + generation，见
    :func:`~forgemesh.controlplane.reconciler.dedup.allocation_idempotency_key`）与取自
    ``spec.resources.accelerator`` 的调度需求：重放得到同一分配。
``Preparing → Deploying``
    以同一幂等键再次 allocate() 取回分配，停止资源之前的部署（见下文），然后
    WorkflowRuntime.start() 启动部署工作流
    （:mod:`forgemesh.controlplane.deployment.workflow`），幂等键为
    :func:`~forgemesh.controlplane.deployment.workflow.deployment_run_key`（uid + generation），
    输入是携带分配设备的 :class:`~forgemesh.controlplane.deployment.launcher.LaunchRequest`。
``Deploying → Verifying → Ready``
    find_runs() 按幂等键前缀查询当前 generation 的运行：实例启动后进入 ``Verifying``；运行
    成功后进入 ``Ready``，写入 ``status.endpoints``（取自 ``launch-instance`` 的结果）与
    ``Ready=True``。运行未结束时每 ``workflow_poll_interval`` 秒查询一次。注入启动器时，写入
    ``Ready`` 之前先确认实例仍在运行，实例已不在运行时重新部署（见“实例丢失”）。

部署与更新中只有进入 ``Ready`` 或 ``Failed`` 的一轮返回 ``done=True``（登记去重键）；其余
各轮返回 ``done=False``，同一 generation 的去重键不会挡住下一轮。

更新已部署的服务
----------------
``Ready`` / ``Degraded → Updating``
    spec 变化（``generation`` 大于 ``observedGeneration``）时 PolicyEngine.check() 授权这次
    更新，然后进入 ``Updating``，endpoint 标记为未就绪；这一轮没有副作用。被策略拦下或缺少
    调度器 / 工作流运行时的一轮不写 status、只记日志（任何写入都会把 ``observedGeneration``
    推进到新的 generation，这次更新便不会再被处理），策略拒绝或等待审批时 30 秒后重新评估。
``Updating → Ready``
    停止之前的部署，按新的 generation 取得分配（见下文）并启动这一代 spec 的部署运行，清空
    ``status.endpoints``；之后像 ``Verifying`` 一样跟踪运行，成功后进入 ``Ready``，写入新实例
    的地址。先停后起：从进入 ``Updating``（网关只向 ``Ready`` 的服务转发）到新实例通过验证，
    服务不可用。更新的运行失败时进入 ``Failed``（之前的实例已经停止）。

部署或更新过程中 spec 变化
--------------------------
新的 generation 有自己的分配幂等键与部署运行。资源已持有的分配（为之前的 generation 建立）
满足新的加速器申请时直接复用；不满足时调度器拒绝复用（``ConflictError``），协调器先停止
资源的实例、释放这个分配，再重新分配（新的申请放不下时停在当前 phase，``AllocationFailed``）。
启动部署运行之前总是先经 release provider 停止资源之前的部署（取消进行中的运行、停止实例），
同一时刻只有一个实例占用分配的设备。``Deploying`` / ``Verifying`` / ``Updating`` 时没有当前
generation 的运行（spec 在部署过程中变化，或运行来自旧版本、幂等键不含 generation）：
停止之前的部署、取得分配并启动当前 generation 的运行，phase 不变，之后跟踪新的运行，旧的
运行不会让服务以旧的 spec 进入 ``Ready``。

实例丢失（``Degraded``）
------------------------
实例已不在运行的 ``Ready`` 服务由
:func:`~forgemesh.controlplane.deployment.recovery.check_instances` 转为 ``Degraded``（控制
平面启动时，以及控制器运行中定期核对时）。spec 没有变化的 ``Degraded`` 服务每一轮：

* 这一代 spec 最近一次部署的运行进行中：每 ``workflow_poll_interval`` 秒查询一次；
* 运行成功、实例在运行（实例回来了，或重新部署成功）：回到 ``Ready``，写入实例地址；
* 运行成功却没有报告实例：与部署中一样进入 ``Failed``，保留分配；
* 否则（实例仍不在运行，或重新部署的运行失败）：停止之前的部署，取得分配（通常复用资源
  持有的分配），以同一代 spec 再启动一次部署运行（幂等键带上次数，见
  :func:`~forgemesh.controlplane.deployment.workflow.deployment_run_key`），清空
  ``status.endpoints``，``Ready=False``（``InstanceLost``）。phase 不变，不重新评估策略：
  这一代 spec 的部署已经授权。

运行成功之后、写入 ``Ready`` 之前实例就已不在运行时（``Verifying`` / ``Updating``，如控制
平面恰好在这时重启）同样重新部署，phase 不变；这次运行失败时照常进入 ``Failed``。

重新部署按资源退避：同一代 spec 的第一次立即开始，之后与上一次开始至少相隔
:data:`REDEPLOY_BACKOFF_INITIAL_SECONDS` 秒，每次翻倍，最多
:data:`REDEPLOY_BACKOFF_MAX_SECONDS` 秒；上一次开始已过
:data:`REDEPLOY_BACKOFF_RESET_SECONDS` 秒时从头开始。等待期间上一次的运行失败时，
``Ready=False``（``WorkflowFailed``）说明原因。退避只记在内存中，控制平面重启之后从头
开始。恢复中只有进入 ``Failed`` 的一轮返回 ``done=True``：同一代 spec 可能多次丢失实例，
回到 ``Ready`` 的一轮不登记去重键；控制器管理器为 ``Degraded`` 使用单独的去重操作
（``recover``），部署进入 ``Ready`` 时登记的去重键不会挡住恢复。

失败
----
* 分配失败：停在当前 phase，``Ready=False``（``AllocationFailed``），10 秒后重试。
* 部署工作流没能启动（运行没能创建或启动）：停在当前 phase，``Ready=False``
  （``WorkflowStartFailed``），保留分配，按指数退避重试；查询运行失败（``WorkflowLookupFailed``）
  与停止之前的部署失败（``StopFailed``）同样退避重试。
* 部署运行失败（某一步用尽重试，工作流已补偿、停止了实例）、被取消或超时：
  ``phase=Failed``、``Ready=False``（``WorkflowFailed``，消息含失败的步骤），释放分配。
  补偿没能停止实例时暂时保留分配并立即重新排队：之后 spec 没有变化的 ``Failed`` 一轮经
  release provider 停止资源的实例，然后释放分配（这一轮不写 status，停止失败时按指数退避
  重试）。资源没有任何部署运行（``WorkflowRunNotFound``，如工作流数据库被重置）或运行
  成功却没有报告实例（运行时不返回步骤输出）时同样进入 ``Failed``：无从确认、无从停止
  实例，分配留到删除资源时释放。
* ``Failed`` 不自动重试部署。spec 变化（``generation`` 大于 ``observedGeneration``）时回到
  ``Pending``（``Ready=False``，原因 ``Pending``，这一轮没有副作用），之后与新建的资源一样
  经过策略校验、分配与部署：还没停止的实例在启动新的部署运行之前停止，仍持有的分配满足
  新的申请时复用。spec 没有变化时删除后重新创建资源即可再次部署。

需要分配或执行工作流、却未注入对应组件时，在任何副作用之前返回 ``Ready=False``
（``SchedulerUnavailable`` / ``WorkflowRuntimeUnavailable``）。没有 ``metadata.uid`` 的
资源报告 ``MissingUid``，不是 ``InferenceService`` 的资源报告 ``NotDeployable``。spec 没有
变化的 ``Ready``，以及 ``Unknown`` 的资源没有可做的事；spec 没有变化的 ``Failed`` 只在仍
持有分配时停止实例、释放分配（见上文）。

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
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import Resource
from forgemesh.core.reconciler import ReconcileContext, Reconciler, ReconcileResult
from forgemesh.core.scheduler import AllocationRef, ResourceRef, ScheduleRequest
from forgemesh.policy.api import PolicyAction, PolicyEngine
from forgemesh.resource.inference import AcceleratorSpec, InferencePhase, InferenceService
from forgemesh.workflow.runtime import RunInfo, RunRef
from forgemesh.workflow.states import StepStatus, WorkflowStatus

from ..deployment import (
    INSTANCE_LOST_REASON,
    LAUNCH_INSTANCE,
    DeploymentRun,
    LaunchedInstance,
    LaunchRequest,
    RuntimeLauncher,
    deployment_inputs,
    deployment_run_key,
    deployment_workflow,
    find_deployment_runs,
    launched_instance,
)
from ..registry.store import DEFAULT_RELEASE_FINALIZER
from .dedup import ProcessedKeyStore, allocation_idempotency_key

logger = logging.getLogger(__name__)

#: 启动或查询部署运行失败后重试间隔的上限（秒）。
MAX_RETRY_DELAY_SECONDS = 60.0
#: 实例丢失后第二次重新部署与第一次开始之间的最短间隔（秒），之后每次翻倍。
REDEPLOY_BACKOFF_INITIAL_SECONDS = 10.0
#: 两次重新部署开始之间最短间隔的上限（秒）。
REDEPLOY_BACKOFF_MAX_SECONDS = 300.0
#: 上一次重新部署开始这么久（秒）之后再丢失实例时，退避从头开始（立即重新部署）。
REDEPLOY_BACKOFF_RESET_SECONDS = 2 * REDEPLOY_BACKOFF_MAX_SECONDS

#: 部署或更新过程中的 phase（``None`` 为尚未观测到 phase），每一轮都要协调。
_DEPLOYMENT_PHASES: frozenset[str | None] = frozenset(
    {
        None,
        InferencePhase.PENDING,
        InferencePhase.SCHEDULING,
        InferencePhase.PREPARING,
        InferencePhase.DEPLOYING,
        InferencePhase.VERIFYING,
        InferencePhase.UPDATING,
    }
)
#: 本轮或之后的轮次还要调用调度器的 phase（``Preparing`` 以同一幂等键取回分配）。
_ALLOCATION_PHASES: frozenset[str | None] = frozenset(
    {None, InferencePhase.PENDING, InferencePhase.SCHEDULING, InferencePhase.PREPARING}
)
#: 服务中的 phase：spec 变化（``generation`` 大于 ``observedGeneration``）时开始更新。
_SERVING_PHASES: frozenset[str | None] = frozenset({InferencePhase.READY, InferencePhase.DEGRADED})


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


def _latest(runs: list[DeploymentRun]) -> DeploymentRun | None:
    """同一代 spec 的各次部署中最近的一次。"""
    return max(runs, key=lambda deployment: deployment.attempt, default=None)


@dataclass
class _Redeploys:
    """资源在本进程中为同一代 spec 重新部署的次数与最近一次开始的时刻（只用于退避）。"""

    generation: int
    count: int
    last_started: float


class ReleaseProvider(Protocol):
    """受控释放 Provider 协议（控制平面侧，不绑定具体运行时插件）。

    职责：在资源进入 ``Terminating`` 后释放其外部副作用（取消进行中的部署、停止推理
    实例等，如 :class:`~forgemesh.controlplane.deployment.DeploymentReleaser`）；协调器在
    启动新的部署运行之前、释放 ``Failed`` 服务的分配之前同样调用它，停止资源之前部署的
    实例。实现方应保证**幂等**：同一资源重复调用不得产生重复副作用。

    ``release`` 正常返回视为释放成功；抛出任意异常视为释放失败——协调器
    将保持 ``Terminating``（或当前 phase）并 requeue 重试，**不会**物理删除资源，也不会
    启动新的部署（GPU 不泄漏也不误删）。协议刻意不依赖插件包类型（``plugins/*``），由
    组装方桥接。
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
        workflow_runtime: 可选工作流运行时（执行部署工作流，须实现 ``find_runs``，返回
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
        launcher: 可选，部署工作流使用的启动器；注入时写入 ``Ready`` 之前以
            :meth:`~forgemesh.controlplane.deployment.RuntimeLauncher.is_running` 确认实例
            仍在运行，否则重新部署（未注入时视为实例在运行）。
        clock: 单调时钟（秒），用于重新部署的退避；默认 :func:`time.monotonic`。

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
        launcher: RuntimeLauncher | None = None,
        clock: Callable[[], float] = time.monotonic,
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
        self._launcher = launcher
        self._clock = clock
        # 已处理的去重键（内存级回退路径；processed_store 注入时以 SQLite
        # 持久化为准——architecture §5.5 不依赖单进程内存状态）。
        self._processed_keys: set[str] = set()
        # 按资源 uid 记录连续的启动 / 查询失败次数，只用于计算退避。
        self._retry_counts: dict[str, int] = {}
        self._redeploys: dict[str, _Redeploys] = {}

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
        if phase == InferencePhase.FAILED:
            return await self._reconcile_failed(ctx)

        diff = self._compute_diff(resource, ctx.observed)
        if not diff["needs_reconcile"]:
            await self._mark_processed(ctx.dedup_key)
            return ReconcileResult(done=True)

        meta = resource.metadata
        update = phase in _SERVING_PHASES and meta.generation > meta.observed_generation
        if phase in (None, InferencePhase.PENDING):
            blocked = await self._authorize(ctx, diff)
            if blocked is not None:
                return blocked
        if update:
            held = await self._update_blocked(ctx, diff)
            if held is not None:
                return held

        # 必须先于任何副作用检查：否则可能分配了资源却没有运行时部署它。
        missing = self._missing_executor(diff)
        if missing is not None:
            reason, message = missing
            return _not_ready(reason, message, requeue_after=None)
        if not meta.uid:
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
        if update:
            return self._begin_update(resource)
        if phase == InferencePhase.DEGRADED:
            return await self._recover(ctx, resource)
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

    async def _update_blocked(
        self, ctx: ReconcileContext, diff: dict[str, Any]
    ) -> ReconcileResult | None:
        """更新开始之前的策略校验与执行组件检查；可以开始时返回 ``None``。

        被拦下的一轮不写 status、只记日志：任何 status 写入都会把 ``observedGeneration``
        推进到新的 generation，服务中的资源之后就不会再处理这次更新。策略拒绝或等待审批
        时稍后重新评估；缺少执行组件时等资源变更或全量同步。
        """
        blocked = await self._authorize(ctx, diff)
        if blocked is not None:
            [condition] = blocked.status_patch["conditions"]
            reason, message = condition["reason"], condition["message"]
            requeue_after = blocked.requeue_after
        else:
            missing = self._missing_executor(diff)
            if missing is None:
                return None
            (reason, message), requeue_after = missing, None
        meta = ctx.resource.metadata
        logger.warning(
            "update not started: reason=%s message=%s kind=%s namespace=%s name=%s uid=%s "
            "generation=%s tenant_id=%s correlation_id=%s",
            reason,
            message,
            ctx.resource.kind,
            meta.namespace,
            meta.name,
            meta.uid,
            meta.generation,
            ctx.tenant_id,
            ctx.correlation_id,
        )
        return ReconcileResult(done=False, requeue_after=requeue_after)

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

    async def _allocation(
        self, ctx: ReconcileContext, service: InferenceService
    ) -> AllocationRef | ReconcileResult:
        """取得服务于当前 spec 的分配；失败时返回停在当前 phase 的结果。

        调度器拒绝复用资源为之前的 generation 建立的分配（不满足新的加速器申请）时，先停止
        资源的实例、释放该分配，再重新分配。
        """
        try:
            return await self._allocate(ctx, service)
        except ConflictError as exc:
            if not await self._holds_previous_allocation(ctx, service):
                return self._allocation_failed(exc)
        except Exception as exc:  # noqa: BLE001 - 分配失败需可重试
            return self._allocation_failed(exc)
        stopped = await self._stop_previous_deployment(ctx, service)
        if stopped is not None:
            return stopped
        await self._release_allocation(ctx, reason="the accelerator request changed")
        try:
            return await self._allocate(ctx, service)
        except Exception as exc:  # noqa: BLE001 - 分配失败需可重试
            return self._allocation_failed(exc)

    async def _holds_previous_allocation(
        self, ctx: ReconcileContext, service: InferenceService
    ) -> bool:
        """资源是否持有以另一个幂等键（之前的 generation）建立的 Active 分配。"""
        get_active = getattr(self._registry, "get_active_allocation", None)
        if not callable(get_active):
            return False
        meta = service.metadata
        held = await get_active(meta.uid, ctx.tenant_id)
        return held is not None and held["idempotency_key"] != allocation_idempotency_key(
            meta.uid, meta.generation
        )

    async def _stop_previous_deployment(
        self, ctx: ReconcileContext, service: InferenceService
    ) -> ReconcileResult | None:
        """经 release provider 停止资源已部署的实例；失败时返回停在当前 phase 的结果。"""
        if self._release_provider is None:
            return None
        try:
            await self._release_provider.release(service)
        except Exception as exc:  # noqa: BLE001 - 停止失败：不启动新的部署，退避重试
            delay = self._next_retry_delay(service.metadata.uid)
            logger.warning(
                "stopping the previous deployment failed (retrying in %.1fs): %s",
                delay,
                _describe(exc),
            )
            return _not_ready(
                "StopFailed",
                f"the previous deployment could not be stopped: {_describe(exc)}",
                requeue_after=delay,
            )
        return None

    async def _start_run(
        self,
        ctx: ReconcileContext,
        service: InferenceService,
        allocation: AllocationRef,
        attempt: int = 1,
    ) -> RunRef | ReconcileResult:
        """启动当前 generation 第 ``attempt`` 次部署的运行；失败时返回停在当前 phase 的结果。"""
        meta = service.metadata
        run_key = deployment_run_key(meta.uid, meta.generation, attempt)
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
            delay = self._next_retry_delay(meta.uid)
            logger.warning(
                "deployment workflow start failed (retrying in %.1fs): %s", delay, _describe(exc)
            )
            return _not_ready("WorkflowStartFailed", _describe(exc), requeue_after=delay)
        self._retry_counts.pop(meta.uid, None)
        return run

    async def _deploy(
        self, ctx: ReconcileContext, service: InferenceService, attempt: int = 1
    ) -> RunRef | ReconcileResult:
        """停止之前的部署，取得分配，启动当前 generation 第 ``attempt`` 次部署的运行。"""
        if self._scheduler is None:
            return _not_ready(
                "SchedulerUnavailable", "no scheduler is configured to allocate resources", None
            )
        stopped = await self._stop_previous_deployment(ctx, service)
        if stopped is not None:
            return stopped
        allocation = await self._allocation(ctx, service)
        if isinstance(allocation, ReconcileResult):
            return allocation
        return await self._start_run(ctx, service, allocation, attempt)

    async def _lookup(
        self, service: InferenceService, generation: int | None = None
    ) -> list[DeploymentRun] | ReconcileResult:
        """资源的部署运行（见 :func:`find_deployment_runs`）；查询失败时返回退避重试的结果。"""
        uid = service.metadata.uid
        try:
            return await find_deployment_runs(self._workflow, uid, generation)
        except Exception as exc:  # noqa: BLE001 - 查询失败：保持当前 phase，退避重试
            delay = self._next_retry_delay(uid)
            logger.warning(
                "deployment workflow lookup failed (retrying in %.1fs): %s",
                delay,
                _describe(exc),
            )
            return _not_ready("WorkflowLookupFailed", _describe(exc), requeue_after=delay)

    async def _schedule(self, ctx: ReconcileContext, service: InferenceService) -> ReconcileResult:
        """``Scheduling`` 一轮：分配加速器后进入 ``Preparing``。"""
        allocation = await self._allocation(ctx, service)
        if isinstance(allocation, ReconcileResult):
            return allocation
        devices = ", ".join(f"{d.node_id}/{d.device_id}" for d in allocation.devices)
        return _progress(
            InferencePhase.PREPARING,
            f"allocation {allocation.allocation_id} holds {devices or 'no devices'}",
            requeue_after=0.0,
        )

    async def _start_deployment(
        self, ctx: ReconcileContext, service: InferenceService
    ) -> ReconcileResult:
        """``Preparing`` 一轮：取回分配，启动当前 generation 的部署运行后进入 ``Deploying``。"""
        allocation = await self._allocation(ctx, service)
        if isinstance(allocation, ReconcileResult):
            return allocation
        runs = await self._lookup(service, service.metadata.generation)
        if isinstance(runs, ReconcileResult):
            return runs
        current = _latest(runs)
        if current is not None:
            # 上一轮已启动运行，只是没能写入 Deploying。
            run_id = current.run.run_id
        else:
            stopped = await self._stop_previous_deployment(ctx, service)
            if stopped is not None:
                return stopped
            started = await self._start_run(ctx, service, allocation)
            if isinstance(started, ReconcileResult):
                return started
            run_id = started.run_id
        return _progress(
            InferencePhase.DEPLOYING,
            f"deployment workflow run {run_id} started",
            requeue_after=self._poll_interval,
        )

    @staticmethod
    def _begin_update(service: InferenceService) -> ReconcileResult:
        """``Ready`` / ``Degraded`` 且 spec 已变化：进入 ``Updating``，endpoint 标记为未就绪。

        这一轮没有副作用，下一轮停止之前的部署、部署新的 spec。
        """
        endpoints = [
            endpoint.model_copy(update={"ready": False}).model_dump(mode="json")
            for endpoint in service.status.endpoints
        ]
        message = f"deploying generation {service.metadata.generation}"
        return ReconcileResult(
            done=False,
            requeue_after=0.0,
            status_patch={
                "phase": InferencePhase.UPDATING.value,
                "endpoints": endpoints,
                "conditions": [_ready_condition("False", InferencePhase.UPDATING.value, message)],
            },
        )

    async def _redeploy(self, ctx: ReconcileContext, service: InferenceService) -> ReconcileResult:
        """``Updating`` 且没有当前 generation 的运行：停止之前的部署，部署当前的 spec。"""
        started = await self._deploy(ctx, service)
        if isinstance(started, ReconcileResult):
            return started
        message = (
            f"deployment workflow run {started.run_id} deploys generation "
            f"{service.metadata.generation}"
        )
        return ReconcileResult(
            done=False,
            requeue_after=self._poll_interval,
            status_patch={
                "endpoints": [],
                "conditions": [_ready_condition("False", InferencePhase.UPDATING.value, message)],
            },
        )

    async def _follow_deployment(
        self, ctx: ReconcileContext, service: InferenceService, phase: str
    ) -> ReconcileResult:
        """``Deploying`` / ``Verifying`` / ``Updating`` 一轮：查询当前 generation 的部署运行并
        推进。"""
        meta = service.metadata
        runs = await self._lookup(service, meta.generation)
        if isinstance(runs, ReconcileResult):
            return runs
        current = _latest(runs)
        if current is None:
            if phase == InferencePhase.UPDATING:
                return await self._redeploy(ctx, service)
            return await self._replace_deployment(ctx, service, phase)
        run = current.run
        instance = launched_instance(run)
        succeeded = run.status is WorkflowStatus.SUCCEEDED and instance is not None
        if (
            succeeded
            and phase != InferencePhase.DEPLOYING
            and not await self._is_running(instance.instance_id)
        ):
            return await self._redeploy_lost(ctx, service, current)
        self._retry_counts.pop(meta.uid, None)
        if succeeded:
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

    async def _replace_deployment(
        self, ctx: ReconcileContext, service: InferenceService, phase: str
    ) -> ReconcileResult:
        """没有当前 generation 的运行：停止之前的部署并部署当前的 spec，phase 不变。"""
        meta = service.metadata
        previous = await self._lookup(service)
        if isinstance(previous, ReconcileResult):
            return previous
        if not previous:
            # 运行记录丢失：无法确认是否有实例在运行，分配留到删除资源时释放。
            run_key = deployment_run_key(meta.uid, meta.generation)
            return await self._fail(
                ctx,
                "WorkflowRunNotFound",
                f"deployment workflow run {run_key!r} not found",
                release_allocation=False,
            )
        started = await self._deploy(ctx, service)
        if isinstance(started, ReconcileResult):
            return started
        message = (
            f"generation {meta.generation} replaces the deployment in progress: "
            f"deployment workflow run {started.run_id} started"
        )
        return ReconcileResult(
            done=False,
            requeue_after=self._poll_interval,
            status_patch={"conditions": [_ready_condition("False", phase, message)]},
        )

    # ------------------------------------------------------------------ #
    # 实例丢失后重新部署（Degraded，以及运行成功之后实例丢失的 Verifying / Updating）
    # ------------------------------------------------------------------ #

    async def _recover(self, ctx: ReconcileContext, service: InferenceService) -> ReconcileResult:
        """``Degraded`` 一轮（spec 没有变化）：实例回来了就回到 ``Ready``，否则重新部署。

        回到 ``Ready`` 的一轮不登记去重键：同一代 spec 之后可能再次丢失实例。运行成功却没有
        报告实例时与部署中一样进入 ``Failed``：重新部署也无从得知、无从停止新的实例。
        """
        meta = service.metadata
        runs = await self._lookup(service, meta.generation)
        if isinstance(runs, ReconcileResult):
            return runs
        current = _latest(runs)
        if current is not None and not current.run.terminal:
            self._retry_counts.pop(meta.uid, None)
            return ReconcileResult(done=False, requeue_after=self._poll_interval)
        if current is not None and current.run.status is WorkflowStatus.SUCCEEDED:
            instance = launched_instance(current.run)
            if instance is None:
                return await self._fail_run(ctx, current.run, None)
            if await self._is_running(instance.instance_id):
                self._retry_counts.pop(meta.uid, None)
                return await self._ready(ctx, instance, record=False)
        return await self._redeploy_lost(ctx, service, current)

    async def _redeploy_lost(
        self, ctx: ReconcileContext, service: InferenceService, current: DeploymentRun | None
    ) -> ReconcileResult:
        """实例已不在运行：按退避等待，然后停止之前的部署，以同一代 spec 再部署一次。

        ``current`` 为这一代 spec 最近一次部署的运行（已结束；这一代 spec 没有运行时为
        ``None``）。phase 不变，也不重新评估策略：这一代 spec 的部署已经授权。上一次重新部署
        的运行失败时，等待期间 ``Ready`` 条件说明失败原因。
        """
        meta = service.metadata
        delay = self._redeploy_delay(meta.uid, meta.generation)
        if delay > 0:
            if current is None or current.run.status is WorkflowStatus.SUCCEEDED:
                return ReconcileResult(done=False, requeue_after=delay)
            run = current.run
            error = run.error or f"deployment workflow run {run.run_id} ended {run.status.value}"
            message = f"{error}; redeploying the service after a backoff"
            return _not_ready("WorkflowFailed", message, delay)
        attempt = 1 if current is None else current.attempt + 1
        started = await self._deploy(ctx, service, attempt)
        if isinstance(started, ReconcileResult):
            return started
        self._record_redeploy(meta.uid, meta.generation)
        logger.warning(
            "redeploying a service whose instance is gone: run_id=%s attempt=%d kind=%s "
            "namespace=%s name=%s uid=%s generation=%s tenant_id=%s correlation_id=%s",
            started.run_id,
            attempt,
            service.kind,
            meta.namespace,
            meta.name,
            meta.uid,
            meta.generation,
            ctx.tenant_id,
            ctx.correlation_id,
        )
        message = (
            f"deployment workflow run {started.run_id} redeploys the service (attempt {attempt})"
        )
        return ReconcileResult(
            done=False,
            requeue_after=self._poll_interval,
            status_patch={
                "endpoints": [],
                "conditions": [_ready_condition("False", INSTANCE_LOST_REASON, message)],
            },
        )

    def _redeploy_delay(self, uid: str, generation: int) -> float:
        """距离可以再次重新部署还要等待的秒数（``0`` 为现在就可以）。

        同一代 spec 的第一次重新部署立即开始；之后与上一次开始至少相隔
        :data:`REDEPLOY_BACKOFF_INITIAL_SECONDS`，每次翻倍，最多
        :data:`REDEPLOY_BACKOFF_MAX_SECONDS`。上一次开始已过
        :data:`REDEPLOY_BACKOFF_RESET_SECONDS` 时从头开始。
        """
        record = self._redeploys.get(uid)
        if record is None or record.generation != generation:
            return 0.0
        elapsed = self._clock() - record.last_started
        if elapsed >= REDEPLOY_BACKOFF_RESET_SECONDS:
            return 0.0
        interval = REDEPLOY_BACKOFF_INITIAL_SECONDS * 2 ** min(record.count - 1, 30)
        return max(0.0, min(interval, REDEPLOY_BACKOFF_MAX_SECONDS) - elapsed)

    def _record_redeploy(self, uid: str, generation: int) -> None:
        now = self._clock()
        record = self._redeploys.get(uid)
        if (
            record is None
            or record.generation != generation
            or now - record.last_started >= REDEPLOY_BACKOFF_RESET_SECONDS
        ):
            self._redeploys[uid] = _Redeploys(generation=generation, count=1, last_started=now)
            return
        record.count += 1
        record.last_started = now

    async def _is_running(self, instance_id: str) -> bool:
        """实例是否在运行；未注入启动器时无从确认，视为在运行。"""
        return self._launcher is None or await self._launcher.is_running(instance_id)

    @staticmethod
    def _verifying(instance: LaunchedInstance, requeue_after: float) -> ReconcileResult:
        return _progress(
            InferencePhase.VERIFYING,
            f"instance {instance.instance_id} launched at {instance.url}",
            requeue_after=requeue_after,
        )

    async def _ready(
        self, ctx: ReconcileContext, instance: LaunchedInstance, *, record: bool = True
    ) -> ReconcileResult:
        """运行成功：进入 ``Ready``，写入实例地址；``record`` 为真时登记去重键（``done``）。"""
        if record:
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
            done=record,
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
                "before the allocation is released"
            )
        return await self._fail(ctx, "WorkflowFailed", message, release_allocation=instance is None)

    async def _fail(
        self, ctx: ReconcileContext, reason: str, message: str, *, release_allocation: bool
    ) -> ReconcileResult:
        """进入 ``Failed``；``release_allocation`` 为真时先释放资源的分配，否则立即重新排队，
        由之后的 ``Failed`` 一轮停止实例、释放分配（见 :meth:`_reclaim`）。"""
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
            requeue_after=None if release_allocation else 0.0,
            status_patch={
                "phase": InferencePhase.FAILED.value,
                "conditions": [_ready_condition("False", reason, message)],
            },
        )

    async def _reconcile_failed(self, ctx: ReconcileContext) -> ReconcileResult:
        """``Failed`` 一轮：spec 变化时回到 ``Pending`` 重新部署，否则释放资源仍持有的分配
        （见 :meth:`_reclaim`）。

        回到 ``Pending`` 的一轮没有副作用，也不评估策略：``Pending`` 一轮像对新建的资源一样
        授权这次部署，之前的实例与分配由之后的部署轮次停止、复用或替换。
        """
        meta = ctx.resource.metadata
        if meta.generation <= meta.observed_generation:
            return await self._reclaim(ctx)
        if meta.uid:
            self._retry_counts.pop(meta.uid, None)
        message = f"the spec changed (generation {meta.generation}); deploying it again"
        return ReconcileResult(
            done=False,
            requeue_after=0.0,
            status_patch={
                "phase": InferencePhase.PENDING.value,
                "endpoints": [],
                "conditions": [_ready_condition("False", InferencePhase.PENDING.value, message)],
            },
        )

    async def _reclaim(self, ctx: ReconcileContext) -> ReconcileResult:
        """spec 没有变化的 ``Failed`` 一轮：资源仍持有分配时停止它的实例，然后释放分配。

        补偿没能停止实例时，进入 ``Failed`` 的一轮保留了分配。这一轮像 ``Terminating`` 一样经
        release provider 停止资源的全部部署，再释放分配；查询或停止失败时不登记去重键，按
        指数退避重试。资源没有部署运行，或有运行成功却没有报告实例时无从停止实例，分配留到
        删除资源时释放。这一轮不写 status。
        """
        uid = ctx.resource.metadata.uid
        get_active = getattr(self._registry, "get_active_allocation", None)
        if (
            uid
            and self._workflow is not None
            and callable(get_active)
            and await get_active(uid, ctx.tenant_id) is not None
        ):
            kept = await self._stop_failed_deployment(ctx)
            if kept is not None:
                return kept
        await self._mark_processed(ctx.dedup_key)
        return ReconcileResult(done=True)

    async def _stop_failed_deployment(self, ctx: ReconcileContext) -> ReconcileResult | None:
        """停止 ``Failed`` 服务的部署并释放它的分配；查询或停止失败时返回退避重试的结果。"""
        service = ctx.resource
        meta = service.metadata
        try:
            runs = await find_deployment_runs(self._workflow, meta.uid)
            stoppable = bool(runs) and all(
                deployment.run.status is not WorkflowStatus.SUCCEEDED
                or launched_instance(deployment.run) is not None
                for deployment in runs
            )
            if stoppable:
                await self._release_provider.release(service)
        except Exception as exc:  # noqa: BLE001 - 保留分配，退避重试
            delay = self._next_retry_delay(meta.uid)
            logger.warning(
                "the deployment of a failed service could not be stopped, its allocation is "
                "kept (retrying in %.1fs): %s; kind=%s namespace=%s name=%s uid=%s "
                "tenant_id=%s correlation_id=%s",
                delay,
                _describe(exc),
                service.kind,
                meta.namespace,
                meta.name,
                meta.uid,
                ctx.tenant_id,
                ctx.correlation_id,
            )
            return ReconcileResult(done=False, requeue_after=delay)
        self._retry_counts.pop(meta.uid, None)
        if stoppable:
            await self._release_allocation(ctx, reason="the failed deployment was stopped")
            return None
        logger.info(
            "the allocation of a failed service is kept until the service is deleted, its "
            "instance cannot be found: kind=%s namespace=%s name=%s uid=%s tenant_id=%s "
            "correlation_id=%s",
            service.kind,
            meta.namespace,
            meta.name,
            meta.uid,
            ctx.tenant_id,
            ctx.correlation_id,
        )
        return None

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
            self._redeploys.pop(resource.metadata.uid, None)

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

        * phase 尚未观测到（``None``）或处于部署 / 更新过程中（``Pending`` 到 ``Verifying``、
          ``Updating``）时需要协调；``Degraded`` 总是需要协调（开始更新或恢复）；
        * ``Ready`` 只在 spec 变化（``generation`` 大于 ``observedGeneration``）时需要协调
          （开始更新）；其余 phase 没有可做的事（:meth:`reconcile` 在此之前处理
          ``Terminating`` 与 ``Failed``）。
        * ``needs_allocation``：本轮或之后的轮次要调用调度器（``Preparing`` 及之前、开始更新
          的一轮，以及可能重新部署的 ``Degraded``）；``needs_workflow``：每一轮都要用到工作流
          运行时；``requirements`` 为传给调度器的需求（见 :func:`_scheduling_requirements`）。

        Args:
            resource: 目标资源（含期望状态与元数据）。
            observed: 实际状态快照（缺少 ``phase`` 时取资源自带的 ``status.phase``）。

        Returns:
            差异描述字典，含 ``needs_reconcile`` / ``operation`` /
            ``needs_allocation`` / ``needs_workflow`` / ``requirements`` 等字段。
        """
        observed_phase = _observed_phase(resource, observed)
        meta = resource.metadata
        if observed_phase == InferencePhase.DEGRADED or (
            observed_phase in _SERVING_PHASES and meta.generation > meta.observed_generation
        ):
            needs_allocation = True
        elif observed_phase in _DEPLOYMENT_PHASES:
            needs_allocation = observed_phase in _ALLOCATION_PHASES
        else:
            return {"needs_reconcile": False, "operation": "noop"}
        return {
            "needs_reconcile": True,
            "operation": "reconcile",
            "needs_allocation": needs_allocation,
            "needs_workflow": True,
            "requirements": _scheduling_requirements(resource),
            "desired_generation": resource.metadata.generation,
            "observed_generation": resource.metadata.observed_generation,
            "observed_phase": observed_phase,
        }


__all__ = [
    "MAX_RETRY_DELAY_SECONDS",
    "REDEPLOY_BACKOFF_INITIAL_SECONDS",
    "REDEPLOY_BACKOFF_MAX_SECONDS",
    "REDEPLOY_BACKOFF_RESET_SECONDS",
    "ReleaseProvider",
    "ReconciliationController",
]
