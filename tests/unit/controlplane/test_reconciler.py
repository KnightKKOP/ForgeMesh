"""ReconciliationController 单元测试。

覆盖：幂等去重键、差异计算、Policy 三态
（allow/deny/require_approval——degraded 已按 architecture 7.5 移除；策略只在离开
Pending 时评估）、按 phase 逐轮推进（Pending → Scheduling → Preparing → Deploying →
Verifying → Ready，写入 ``status.endpoints``）、分配请求的资源身份与幂等键
（uid + generation）、调度需求取自 ``spec.resources.accelerator``、部署工作流的启动
（按 uid + generation 的幂等键，输入携带分配）与启动 / 查询 / 停止失败时的退避、部署过程中
spec 变化（停止之前的部署、加速器申请变化时重新分配）、运行失败时进入 ``Failed`` 以及
分配的去留（补偿没能停止的实例在之后的 ``Failed`` 一轮停止，然后释放分配）、实例丢失后按
退避重新部署、缺少调度器 / 工作流运行时时如实报告 ``Ready=False``、
幂等性，以及 Terminating 受控删除闭环
（ReleaseProvider 释放 → 移除 finalizer → 按 uid 释放 Allocation → 物理删除）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from forgemesh.controlplane.deployment import (
    DEPLOYMENT_WORKFLOW,
    LAUNCH_INSTANCE,
    PREPARE_MODEL,
    LaunchedInstance,
    LaunchRequest,
    deployment_run_key,
)
from forgemesh.controlplane.reconciler import (
    MAX_RETRY_DELAY_SECONDS,
    REDEPLOY_BACKOFF_INITIAL_SECONDS,
    ProcessedKeyStore,
    ReconciliationController,
    allocation_idempotency_key,
    compute_dedup_key,
)
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.controlplane.scheduler import FirstFitScheduler, Inventory
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.reconciler import ReconcileContext
from forgemesh.core.scheduler import AllocatedDevice, AllocationRef, ResourceRef, ScheduleRequest
from forgemesh.policy.api import PolicyDecision
from forgemesh.resource.inference import (
    AcceleratorSpec,
    EndpointProtocol,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)
from forgemesh.workflow.runtime import RunInfo, RunRef, WorkflowDefinition
from forgemesh.workflow.states import StepStatus, WorkflowStatus

# ---------------------------------------------------------------------- #
# 测试辅助
# ---------------------------------------------------------------------- #

ALLOW = PolicyDecision(decision="allow", reason="ok")

#: ``uid-1`` 第 1 代 spec 的部署运行的幂等键。
RUN_KEY = deployment_run_key("uid-1", 1)

INSTANCE = LaunchedInstance(
    instance_id="instance-1", url="http://10.0.0.5:8000", protocol=EndpointProtocol.OPENAI_CHAT
)


def make_resource(
    kind: str = "InferenceService",
    name: str = "svc-1",
    generation: int = 1,
    observed_generation: int = 0,
    tenant_id: str = "tenant-1",
    uid: str | None = "uid-1",
    namespace: str | None = None,
) -> Resource:
    """构造测试用 Resource。"""
    return Resource(
        kind=kind,
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            uid=uid,
            generation=generation,
            observed_generation=observed_generation,
            tenant_id=tenant_id,
        ),
    )


def make_inference_service(
    name: str = "svc-1",
    tenant_id: str = "tenant-1",
    phase: InferencePhase = InferencePhase.READY,
    accelerator: AcceleratorSpec | None = None,
    uid: str | None = "uid-1",
    namespace: str | None = None,
    generation: int = 1,
    observed_generation: int = 0,
) -> InferenceService:
    """构造带 spec/status 的 InferenceService（phase 由 status 携带）。"""
    return InferenceService(
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            uid=uid,
            tenant_id=tenant_id,
            generation=generation,
            observed_generation=observed_generation,
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=accelerator
                or AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=phase),
    )


def make_context(
    resource: Resource,
    observed: dict[str, Any] | None = None,
    dedup_key: str = "key-1",
    tenant_id: str = "tenant-1",
    correlation_id: str = "corr-1",
) -> ReconcileContext:
    """构造测试用 ReconcileContext。"""
    return ReconcileContext(
        resource=resource,
        observed=observed or {},
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        dedup_key=dedup_key,
    )


def make_service_context(service: InferenceService, dedup_key: str = "key-1") -> ReconcileContext:
    """以 ``service.status.phase`` 为观测 phase 的协调上下文（与控制器管理器一致）。"""
    return make_context(service, {"phase": service.status.phase}, dedup_key=dedup_key)


def make_run(
    status: WorkflowStatus,
    launch: StepStatus = StepStatus.SUCCEEDED,
    *,
    error: str | None = None,
    outputs: bool = True,
) -> RunInfo:
    """部署运行的快照：``launch`` 为 launch-instance 的状态，成功过时输出 :data:`INSTANCE`。"""
    launched = launch in {
        StepStatus.SUCCEEDED,
        StepStatus.COMPENSATING,
        StepStatus.COMPENSATED,
        StepStatus.COMPENSATION_FAILED,
    }
    return RunInfo(
        run_id="run-1",
        status=status,
        steps={PREPARE_MODEL: StepStatus.SUCCEEDED, LAUNCH_INSTANCE: launch},
        outputs={LAUNCH_INSTANCE: INSTANCE.model_dump(mode="json")} if launched and outputs else {},
        error=error,
    )


class MockPolicyEngine:
    """Mock PolicyEngine for testing."""

    def __init__(self, result: PolicyDecision) -> None:
        self._result = result

    async def check(self, ctx, action):  # noqa: ANN001
        return self._result


class SequencePolicyEngine:
    """按调用顺序依次返回预设决策（最后一个决策重复使用）。"""

    def __init__(self, *decisions: str) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    async def check(self, ctx, action):  # noqa: ANN001
        decision = self._decisions[min(self.calls, len(self._decisions) - 1)]
        self.calls += 1
        return PolicyDecision(decision=decision, reason=f"call-{self.calls}")


class MockScheduler:
    """Mock Scheduler：记录请求，返回归属请求方 uid、持有 ``node-a/gpu-0`` 的分配。"""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.allocate_called = False
        self.allocate_count = 0
        self.requests: list[ScheduleRequest] = []

    async def allocate(self, req: ScheduleRequest) -> AllocationRef:
        self.allocate_called = True
        self.allocate_count += 1
        self.requests.append(req)
        if self._fail:
            raise RuntimeError("allocation failed")
        return AllocationRef(
            allocation_id="alloc-1",
            resource_id=req.resource_ref.uid,
            devices=[AllocatedDevice(node_id="node-a", device_id="gpu-0")],
        )


class FlakyScheduler(MockScheduler):
    """首次 allocate 失败，之后成功。"""

    async def allocate(self, req: ScheduleRequest) -> AllocationRef:
        self._fail = self.allocate_count == 0
        return await super().allocate(req)


class StubWorkflowRuntime:
    """Stub WorkflowRuntime：记录 start 的调用与 find_runs 查询的前缀。

    ``runs`` 是幂等键到运行快照的映射，``find_runs`` 返回其中键以前缀开头的项。

    Args:
        run: 部署运行的快照；``None`` 表示运行不存在。
        start_failures: 前几次 ``start`` 抛出异常。
        key: ``run`` 预先登记的幂等键；``None`` 时 ``run`` 在成功 ``start`` 之后才登记在
            启动所用的键下。
    """

    def __init__(
        self, run: RunInfo | None = None, start_failures: int = 0, key: str | None = RUN_KEY
    ) -> None:
        self.run = run
        self.start_failures = start_failures
        self.lookup_error: Exception | None = None
        self.started: list[tuple[WorkflowDefinition, dict[str, Any], str]] = []
        self.lookups: list[str] = []
        self.runs: dict[str, RunInfo] = {} if run is None or key is None else {key: run}

    @property
    def idempotency_keys(self) -> list[str]:
        return [key for _, _, key in self.started]

    async def start(
        self, defn: WorkflowDefinition, inputs: dict[str, Any], idempotency_key: str
    ) -> RunRef:
        self.started.append((defn, inputs, idempotency_key))
        if self.start_failures:
            self.start_failures -= 1
            raise RuntimeError("workflow store is locked")
        if self.run is not None:
            self.runs.setdefault(idempotency_key, self.run)
        return RunRef(run_id="run-1", definition_version=1)

    async def find_runs(self, key_prefix: str) -> dict[str, RunInfo]:
        self.lookups.append(key_prefix)
        if self.lookup_error is not None:
            raise self.lookup_error
        return {key: run for key, run in self.runs.items() if key.startswith(key_prefix)}


class StubReleaseProvider:
    """Stub ReleaseProvider：记录调用，可配置抛异常模拟释放失败。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.released: list[str] = []

    async def release(self, resource: Resource) -> None:
        self.released.append(resource.metadata.name)
        if self.fail:
            raise RuntimeError("release failed: gpu driver error")


class SnapshotReleaseProvider(StubReleaseProvider):
    """记录每次 release 时工作流已启动的运行，用来检查先停止之前的部署、后启动新的运行。"""

    def __init__(self, workflow: StubWorkflowRuntime) -> None:
        super().__init__()
        self._workflow = workflow
        self.started_before: list[list[str]] = []

    async def release(self, resource: Resource) -> None:
        self.started_before.append(list(self._workflow.idempotency_keys))
        await super().release(resource)


def _controller(
    *,
    registry: Any = None,
    policy: Any = None,
    scheduler: Any = None,
    workflow: Any = None,
    release: Any = None,
    **kwargs: Any,
) -> ReconciliationController:
    """协调器；注入工作流运行时时一并注入 release provider（构造要求）。"""
    if workflow is not None and release is None:
        release = StubReleaseProvider()
    return ReconciliationController(
        registry=registry,
        policy_engine=policy or MockPolicyEngine(ALLOW),
        scheduler=scheduler,
        workflow_runtime=workflow,
        release_provider=release,
        **kwargs,
    )


async def _deploy(
    controller: ReconciliationController, service: InferenceService
) -> list[dict[str, Any]]:
    """像控制器管理器一样逐轮协调 ``service``（同一去重键），把每轮写入的 phase 应用到
    对象上，直到 ``done``；返回各轮的 status 补丁。"""
    patches = []
    for _ in range(10):
        result = await controller.reconcile(make_service_context(service))
        patches.append(result.status_patch)
        if "phase" in result.status_patch:
            service.status.phase = InferencePhase(result.status_patch["phase"])
        if result.done:
            return patches
    raise AssertionError(f"the deployment did not settle: {patches}")


def _allocate(registry: SQLiteRegistry, service: InferenceService, device_id: str = "gpu-0"):
    """为 ``service`` 建立持有一个设备的 Active 分配（归属其 uid）。"""
    meta = service.metadata
    owner = ResourceRef(kind=service.kind, namespace=meta.namespace, name=meta.name, uid=meta.uid)
    device = AllocatedDevice(node_id="node-a", device_id=device_id)
    return asyncio.run(registry.create_allocation(meta.tenant_id, owner, f"{meta.uid}:1", [device]))


def _allocated_service(
    phase: InferencePhase, observed_generation: int = 0
) -> tuple[SQLiteRegistry, InferenceService]:
    """注册表中处于 ``phase`` 的服务（第 1 代 spec），它持有一个设备的 Active 分配。"""
    registry = SQLiteRegistry()
    service = make_inference_service(phase=phase, observed_generation=observed_generation)
    asyncio.run(registry.put(service))
    _allocate(registry, service)
    return registry, service


def _not_ready(reason: str, message: str) -> dict[str, Any]:
    return {
        "conditions": [{"type": "Ready", "status": "False", "reason": reason, "message": message}]
    }


def _progress(phase: str, message: str) -> dict[str, Any]:
    return {"phase": phase, **_not_ready(phase, message)}


# ---------------------------------------------------------------------- #
# 去重键测试
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_dedup_key_deterministic():
    """相同输入产生相同 dedup_key。"""
    k1 = compute_dedup_key("Node", "default", "node-1", "uid-1", 3, "reconcile")
    k2 = compute_dedup_key("Node", "default", "node-1", "uid-1", 3, "reconcile")
    assert k1.key == k2.key
    assert k1.resource_kind == k2.resource_kind
    assert k1.resource_name == k2.resource_name
    assert k1.generation == k2.generation
    assert k1.operation == k2.operation


@pytest.mark.unit
def test_dedup_key_different_for_different_resources():
    """不同资源产生不同 key。"""
    k1 = compute_dedup_key("Node", "default", "node-1", "uid-1", 3, "reconcile")
    k2 = compute_dedup_key("Node", "default", "node-2", "uid-2", 3, "reconcile")
    assert k1.key != k2.key


# ---------------------------------------------------------------------- #
# 差异计算测试
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_compute_diff_pending_needs_reconcile():
    """phase=Pending → needs_reconcile=True。"""
    resource = make_resource(generation=1, observed_generation=0)
    diff = ReconciliationController._compute_diff(resource, {"phase": "Pending"})
    assert diff["needs_reconcile"] is True
    assert diff["needs_allocation"] is True
    assert diff["needs_workflow"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("phase", "needs_allocation"),
    [
        (None, True),
        ("Pending", True),
        ("Scheduling", True),
        ("Preparing", True),
        ("Deploying", False),
        ("Verifying", False),
        ("Updating", False),
    ],
)
def test_compute_diff_needs_the_scheduler_until_the_workflow_starts(phase, needs_allocation):
    """部署与更新过程中的每一轮都要用到工作流运行时；Preparing 及之前还要用到调度器
    （Updating 在部署时自行检查）。"""
    observed = {} if phase is None else {"phase": phase}
    diff = ReconciliationController._compute_diff(make_resource(), observed)
    assert diff["needs_reconcile"] is True
    assert diff["needs_allocation"] is needs_allocation
    assert diff["needs_workflow"] is True


@pytest.mark.unit
@pytest.mark.parametrize("phase", ["Ready", "Degraded"])
def test_compute_diff_starts_an_update_once_the_spec_of_a_serving_service_changed(phase):
    """服务中的资源 generation 大于 observedGeneration 时需要协调（开始更新），这一轮要求
    调度器与工作流运行时都已注入。"""
    changed = make_resource(generation=2, observed_generation=1)

    diff = ReconciliationController._compute_diff(changed, {"phase": phase})

    assert (diff["needs_reconcile"], diff["needs_allocation"], diff["needs_workflow"]) == (
        True,
        True,
        True,
    )


@pytest.mark.unit
def test_compute_diff_leaves_a_ready_service_alone_until_its_spec_changes():
    unchanged = make_resource(generation=2, observed_generation=2)
    assert ReconciliationController._compute_diff(unchanged, {"phase": "Ready"}) == {
        "needs_reconcile": False,
        "operation": "noop",
    }


@pytest.mark.unit
def test_compute_diff_always_reconciles_a_degraded_service():
    """spec 没有变化的 Degraded 同样需要协调（恢复时可能重新部署），需要调度器与工作流运行时。"""
    unchanged = make_resource(generation=2, observed_generation=2)

    diff = ReconciliationController._compute_diff(unchanged, {"phase": "Degraded"})

    assert (diff["needs_reconcile"], diff["needs_allocation"], diff["needs_workflow"]) == (
        True,
        True,
        True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("phase", ["Failed", "Unknown"])
def test_compute_diff_has_nothing_to_do_outside_a_deployment(phase):
    """Failed / Unknown 没有可做的事，新 generation 也一样。"""
    resource = make_resource(generation=2, observed_generation=1)
    diff = ReconciliationController._compute_diff(resource, {"phase": phase})
    assert diff == {"needs_reconcile": False, "operation": "noop"}


@pytest.mark.unit
def test_compute_diff_falls_back_to_the_phase_in_the_status():
    """观测快照缺少 phase 时取资源自带的 status.phase。"""
    service = make_inference_service(phase=InferencePhase.DEPLOYING)
    diff = ReconciliationController._compute_diff(service, {})
    assert diff["observed_phase"] == "Deploying"
    assert diff["needs_allocation"] is False


@pytest.mark.unit
def test_compute_diff_takes_requirements_from_the_accelerator_request():
    """调度需求取自 spec.resources.accelerator（type / count / minMemoryGiB）。"""
    service = make_inference_service(
        phase=InferencePhase.PENDING,
        accelerator=AcceleratorSpec(type="nvidia-gpu", count=2, minMemoryGiB=40),
    )

    diff = ReconciliationController._compute_diff(service, {"phase": "Pending"})

    assert diff["requirements"] == {
        "accelerator": {"type": "nvidia-gpu", "count": 2, "minMemoryGiB": 40}
    }


@pytest.mark.unit
def test_compute_diff_without_an_accelerator_request_has_no_requirements():
    """没有强类型 spec 的资源（基类 Resource）没有调度需求。"""
    diff = ReconciliationController._compute_diff(make_resource(), {"phase": "Pending"})

    assert diff["requirements"] == {}


# ---------------------------------------------------------------------- #
# 构造约束、Policy 与缺少执行组件
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_workflow_runtime_needs_a_release_provider():
    """没有 release provider，删除资源会留下运行中的实例：拒绝构造。"""
    with pytest.raises(ValueError, match="release provider"):
        ReconciliationController(
            registry=None,
            policy_engine=MockPolicyEngine(ALLOW),
            workflow_runtime=StubWorkflowRuntime(),
        )


@pytest.mark.unit
@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_the_workflow_poll_interval_must_be_positive(interval):
    with pytest.raises(ValueError, match="workflow_poll_interval"):
        _controller(workflow=StubWorkflowRuntime(), workflow_poll_interval=interval)


@pytest.mark.unit
def test_reconcile_no_diff_returns_done():
    """observed 已是 Ready 且 generation 匹配 → done=True。"""
    resource = make_resource(generation=1, observed_generation=1)
    observed = {"phase": "Ready"}
    ctx = make_context(resource, observed)
    controller = ReconciliationController(registry=None, policy_engine=MockPolicyEngine(ALLOW))
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is True
    assert result.requeue_after is None


@pytest.mark.unit
def test_reconcile_policy_deny_stops():
    """Policy 返回 deny → done=False, requeue_after=30, conditions 有 PolicyDenied。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    deny = PolicyDecision(decision="deny", reason="not allowed")
    controller = ReconciliationController(registry=None, policy_engine=MockPolicyEngine(deny))
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is False
    assert result.requeue_after == 30.0
    assert result.status_patch == _not_ready("PolicyDenied", "not allowed")


@pytest.mark.unit
def test_policy_allow_moves_a_pending_service_to_scheduling():
    """Policy 放行 → 只做一个转移 Pending → Scheduling（尚未分配、未启动工作流），立即重新排队。"""
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime()
    controller = _controller(scheduler=scheduler, workflow=workflow)
    ctx = make_service_context(make_inference_service(phase=InferencePhase.PENDING))

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (False, 0.0)
    assert result.status_patch == _progress("Scheduling", "allocating accelerators")
    assert scheduler.allocate_called is False
    assert workflow.started == []
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_policy_is_evaluated_only_when_leaving_pending():
    """策略在 Pending → Scheduling 时授权这次部署，之后各轮不再评估。"""
    policy = SequencePolicyEngine("allow", "deny")
    controller = _controller(
        policy=policy,
        scheduler=MockScheduler(),
        workflow=StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED), key=None),
    )

    patches = asyncio.run(_deploy(controller, make_inference_service(phase=InferencePhase.PENDING)))

    assert policy.calls == 1
    assert patches[-1]["phase"] == "Ready"


@pytest.mark.unit
@pytest.mark.parametrize(
    "phase", [InferencePhase.PENDING, InferencePhase.SCHEDULING, InferencePhase.PREPARING]
)
def test_reconcile_without_scheduler_reports_unavailable(phase):
    """需要分配但未注入调度器 → Ready=False（SchedulerUnavailable），停在当前 phase。"""
    workflow = StubWorkflowRuntime()
    controller = _controller(workflow=workflow)
    ctx = make_service_context(make_inference_service(phase=phase))

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (False, None)
    assert result.status_patch == _not_ready(
        "SchedulerUnavailable", "no scheduler is configured to allocate resources"
    )
    assert workflow.started == []
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
@pytest.mark.parametrize(
    "phase",
    [
        InferencePhase.PENDING,
        InferencePhase.SCHEDULING,
        InferencePhase.PREPARING,
        InferencePhase.DEPLOYING,
        InferencePhase.VERIFYING,
        InferencePhase.UPDATING,
    ],
)
def test_reconcile_without_workflow_runtime_does_not_allocate(phase):
    """需要部署但未注入工作流运行时 → WorkflowRuntimeUnavailable，且不先分配资源。"""
    ctx = make_service_context(make_inference_service(phase=phase))
    scheduler = MockScheduler()
    controller = _controller(scheduler=scheduler)

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (False, None)
    assert result.status_patch == _not_ready(
        "WorkflowRuntimeUnavailable",
        "no workflow runtime is configured to run the deployment workflow",
    )
    assert scheduler.allocate_called is False
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_reconcile_policy_require_approval_blocks():
    """Policy 返回 require_approval → 不产生副作用，requeue 等人工审批。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    approval = PolicyDecision(
        decision="require_approval", reason="high-risk action requires approval"
    )
    scheduler = MockScheduler()
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(approval), scheduler=scheduler
    )
    result = asyncio.run(controller.reconcile(ctx))
    # 审批未通过：不完成、不分配（高风险不允许绕过审批，architecture 7.5）
    assert result.done is False
    assert result.requeue_after == 30.0
    assert scheduler.allocate_called is False
    conditions = result.status_patch["conditions"]
    assert any(c["status"] == "Unknown" for c in conditions)
    assert any(c["reason"] == "PolicyApprovalRequired" for c in conditions)


def _processed_store_variants():
    return [
        pytest.param(lambda: None, id="in-memory-keys"),
        pytest.param(lambda: ProcessedKeyStore(":memory:"), id="processed-store"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("first_decision", ["deny", "require_approval"])
@pytest.mark.parametrize("make_store", _processed_store_variants())
def test_reconcile_requeue_after_policy_block_is_reevaluated(first_decision, make_store):
    """deny / require_approval 未产生副作用：requeue 回来必须重新评估策略。"""
    ctx = make_service_context(make_inference_service(phase=InferencePhase.PENDING))
    policy = SequencePolicyEngine(first_decision, "allow")
    controller = _controller(
        policy=policy,
        scheduler=MockScheduler(),
        workflow=StubWorkflowRuntime(),
        processed_store=make_store(),
    )

    blocked = asyncio.run(controller.reconcile(ctx))
    assert blocked.done is False
    assert "phase" not in blocked.status_patch

    retried = asyncio.run(controller.reconcile(ctx))
    assert policy.calls == 2
    assert retried.status_patch["phase"] == "Scheduling"


@pytest.mark.unit
@pytest.mark.parametrize(
    "phase",
    [
        InferencePhase.PENDING,
        InferencePhase.SCHEDULING,
        InferencePhase.PREPARING,
        InferencePhase.DEPLOYING,
    ],
)
def test_resource_without_uid_is_not_deployed(phase):
    """没有 uid 就无法幂等分配与部署：Ready=False（MissingUid），不分配、不启动或查询工作流、
    不登记去重键。"""
    ctx = make_service_context(make_inference_service(phase=phase, uid=None))
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime()
    controller = _controller(scheduler=scheduler, workflow=workflow)

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (False, None)
    assert result.status_patch == _not_ready(
        "MissingUid", "metadata.uid is required to allocate resources"
    )
    assert scheduler.allocate_called is False
    assert (workflow.started, workflow.lookups) == ([], [])
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_resources_without_a_deployment_spec_are_not_deployed():
    """基类资源（如未登记的 kind）没有部署所需的 spec：Ready=False（NotDeployable），
    不写 phase、不分配。"""
    scheduler = MockScheduler()
    controller = _controller(scheduler=scheduler, workflow=StubWorkflowRuntime())

    result = asyncio.run(controller.reconcile(make_context(make_resource(kind="Widget"))))

    assert (result.done, result.requeue_after) == (False, None)
    assert result.status_patch == _not_ready(
        "NotDeployable", "Widget resources have no deployment spec"
    )
    assert scheduler.allocate_called is False


# ---------------------------------------------------------------------- #
# 逐轮推进：Scheduling → Preparing → Deploying → Verifying → Ready
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_deployment_moves_one_phase_per_pass():
    """每轮一个合法转移直到 Ready：两次分配请求同一幂等键（Preparing 取回分配），
    工作流只启动一次。"""
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED), key=None)
    controller = _controller(scheduler=scheduler, workflow=workflow)

    patches = asyncio.run(_deploy(controller, make_inference_service(phase=InferencePhase.PENDING)))

    assert [patch["phase"] for patch in patches] == [
        "Scheduling",
        "Preparing",
        "Deploying",
        "Verifying",
        "Ready",
    ]
    assert [req.idempotency_key for req in scheduler.requests] == ["uid-1:1", "uid-1:1"]
    assert workflow.idempotency_keys == [RUN_KEY]
    assert patches[-1]["endpoints"] == [INSTANCE.endpoint().model_dump(mode="json")]


@pytest.mark.unit
def test_scheduling_allocates_and_moves_to_preparing():
    """Scheduling：分配加速器后进入 Preparing，立即重新排队。"""
    scheduler = MockScheduler()
    controller = _controller(scheduler=scheduler, workflow=StubWorkflowRuntime())
    ctx = make_service_context(make_inference_service(phase=InferencePhase.SCHEDULING))

    result = asyncio.run(controller.reconcile(ctx))

    assert scheduler.allocate_count == 1
    assert (result.done, result.requeue_after) == (False, 0.0)
    assert result.status_patch == _progress("Preparing", "allocation alloc-1 holds node-a/gpu-0")


@pytest.mark.unit
def test_allocation_request_identifies_the_resource_and_its_generation():
    """分配请求携带资源身份（含 uid）与由 uid + generation 派生的幂等键。"""
    service = make_inference_service(
        phase=InferencePhase.SCHEDULING, generation=3, namespace="team-a"
    )
    scheduler = MockScheduler()

    asyncio.run(
        _controller(scheduler=scheduler, workflow=StubWorkflowRuntime()).reconcile(
            make_service_context(service)
        )
    )

    [req] = scheduler.requests
    assert req.tenant_id == "tenant-1"
    assert req.resource_ref == ResourceRef(
        kind="InferenceService", namespace="team-a", name="svc-1", uid="uid-1"
    )
    assert req.idempotency_key == allocation_idempotency_key("uid-1", 3) == "uid-1:3"


@pytest.mark.unit
def test_allocation_request_carries_the_accelerator_request():
    """调度器收到的需求即 InferenceService 的加速器申请。"""
    service = make_inference_service(
        phase=InferencePhase.SCHEDULING,
        accelerator=AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=24),
    )
    scheduler = MockScheduler()

    asyncio.run(
        _controller(scheduler=scheduler, workflow=StubWorkflowRuntime()).reconcile(
            make_service_context(service)
        )
    )

    [req] = scheduler.requests
    assert req.requirements == {
        "accelerator": {"type": "nvidia-gpu", "count": 1, "minMemoryGiB": 24}
    }


@pytest.mark.unit
def test_replaying_a_generation_requests_the_same_allocation():
    """去重键丢失后（如未持久化的进程重启）重放同一 generation 使用同一幂等键；
    新 generation 与同名重建的资源（新 uid）得到新键。"""
    scheduler = MockScheduler()
    for service in (
        make_inference_service(phase=InferencePhase.SCHEDULING),
        make_inference_service(phase=InferencePhase.SCHEDULING),
        make_inference_service(phase=InferencePhase.SCHEDULING, generation=2),
        make_inference_service(phase=InferencePhase.SCHEDULING, uid="uid-2"),
    ):
        controller = _controller(scheduler=scheduler, workflow=StubWorkflowRuntime())
        asyncio.run(controller.reconcile(make_service_context(service)))

    assert [req.idempotency_key for req in scheduler.requests] == [
        "uid-1:1",
        "uid-1:1",
        "uid-1:2",
        "uid-2:1",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("phase", [InferencePhase.SCHEDULING, InferencePhase.PREPARING])
def test_reconcile_allocation_failure(phase):
    """scheduler.allocate 抛异常 → 停在当前 phase，Ready=False（AllocationFailed），
    10 秒后重试，不启动工作流。"""
    workflow = StubWorkflowRuntime()
    controller = _controller(scheduler=MockScheduler(fail=True), workflow=workflow)

    result = asyncio.run(
        controller.reconcile(make_service_context(make_inference_service(phase=phase)))
    )

    assert (result.done, result.requeue_after) == (False, 10.0)
    assert result.status_patch == _not_ready("AllocationFailed", "allocation failed")
    assert workflow.started == []


@pytest.mark.unit
@pytest.mark.parametrize("make_store", _processed_store_variants())
def test_reconcile_requeue_after_allocation_failure_retries_allocation(make_store):
    """分配失败未产生分配、未登记去重键：requeue 回来必须重新尝试分配。"""
    ctx = make_service_context(make_inference_service(phase=InferencePhase.SCHEDULING))
    scheduler = FlakyScheduler()
    controller = _controller(
        scheduler=scheduler, workflow=StubWorkflowRuntime(), processed_store=make_store()
    )

    failed = asyncio.run(controller.reconcile(ctx))
    assert failed.done is False

    retried = asyncio.run(controller.reconcile(ctx))
    assert retried.status_patch["phase"] == "Preparing"
    assert scheduler.allocate_count == 2


@pytest.mark.unit
def test_preparing_starts_the_deployment_workflow_with_the_allocation():
    """Preparing：以同一幂等键取回分配，以 uid + generation 的幂等键启动部署工作流，输入
    携带分配的设备与 spec；进入 Deploying，按 workflow_poll_interval 重新排队。"""
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime()
    service = make_inference_service(
        phase=InferencePhase.PREPARING, namespace="team-a", generation=2
    )
    controller = _controller(scheduler=scheduler, workflow=workflow, workflow_poll_interval=0.5)

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert [req.idempotency_key for req in scheduler.requests] == ["uid-1:2"]
    [(definition, inputs, key)] = workflow.started
    assert (definition.name, key) == (DEPLOYMENT_WORKFLOW, "deployment:uid-1:2")
    assert LaunchRequest.model_validate(inputs["request"]) == LaunchRequest(
        instance_key="deployment:uid-1:2",
        tenant_id="tenant-1",
        resource=ResourceRef(
            kind="InferenceService", namespace="team-a", name="svc-1", uid="uid-1"
        ),
        spec=service.spec,
        allocation_id="alloc-1",
        devices=(AllocatedDevice(node_id="node-a", device_id="gpu-0"),),
    )
    assert (result.done, result.requeue_after) == (False, 0.5)
    assert result.status_patch == _progress("Deploying", "deployment workflow run run-1 started")


@pytest.mark.unit
def test_workflow_start_failure_keeps_preparing_and_backs_off():
    """启动失败：停在 Preparing、Ready=False（WorkflowStartFailed），保留分配、不登记去重键，
    按指数退避重试（封顶）；成功启动后退避从头开始。"""
    registry, service = _allocated_service(InferencePhase.PREPARING)
    workflow = StubWorkflowRuntime(start_failures=8)
    controller = _controller(
        registry=registry, scheduler=MockScheduler(), workflow=workflow, workflow_poll_interval=1.0
    )
    ctx = make_service_context(service)

    failures = [asyncio.run(controller.reconcile(ctx)) for _ in range(8)]
    started = asyncio.run(controller.reconcile(ctx))
    workflow.start_failures = 1
    failed_again = asyncio.run(controller.reconcile(ctx))

    assert [result.requeue_after for result in failures] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        32.0,
        MAX_RETRY_DELAY_SECONDS,
        MAX_RETRY_DELAY_SECONDS,
    ]
    assert {result.done for result in failures} == {False}
    assert failures[0].status_patch == _not_ready("WorkflowStartFailed", "workflow store is locked")
    assert started.status_patch["phase"] == "Deploying"
    assert failed_again.requeue_after == 1.0
    assert workflow.idempotency_keys == [RUN_KEY] * 10
    assert ctx.dedup_key not in controller._processed_keys
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("phase", "run"),
    [
        (InferencePhase.DEPLOYING, make_run(WorkflowStatus.PENDING, StepStatus.PENDING)),
        (InferencePhase.DEPLOYING, make_run(WorkflowStatus.RUNNING, StepStatus.RUNNING)),
        (InferencePhase.DEPLOYING, make_run(WorkflowStatus.COMPENSATING, StepStatus.SUCCEEDED)),
        (InferencePhase.VERIFYING, make_run(WorkflowStatus.RUNNING, StepStatus.SUCCEEDED)),
        (InferencePhase.VERIFYING, make_run(WorkflowStatus.COMPENSATING, StepStatus.COMPENSATING)),
    ],
)
def test_an_unfinished_run_is_polled_without_writing_status(phase, run):
    """运行未结束：不写 status、不登记去重键，按 workflow_poll_interval 重新排队；
    正在补偿的运行不进入 Verifying。"""
    workflow = StubWorkflowRuntime(run=run)
    controller = _controller(
        scheduler=MockScheduler(), workflow=workflow, workflow_poll_interval=0.5
    )
    ctx = make_service_context(make_inference_service(phase=phase))

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after, result.status_patch) == (False, 0.5, {})
    assert workflow.lookups == [RUN_KEY]
    assert workflow.started == []
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_deploying_moves_to_verifying_once_the_instance_is_launched():
    """launch-instance 成功、运行仍在验证：Deploying → Verifying。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.RUNNING, StepStatus.SUCCEEDED))
    controller = _controller(
        scheduler=MockScheduler(), workflow=workflow, workflow_poll_interval=0.5
    )
    ctx = make_service_context(make_inference_service(phase=InferencePhase.DEPLOYING))

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (False, 0.5)
    assert result.status_patch == _progress(
        "Verifying", "instance instance-1 launched at http://10.0.0.5:8000"
    )


@pytest.mark.unit
def test_a_succeeded_run_makes_the_service_ready_with_its_endpoint():
    """运行成功：Deploying 先进入 Verifying（立即重新排队），Verifying 进入 Ready，写入实例
    地址与 Ready=True 并登记去重键，之后的重放没有副作用。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    controller = _controller(scheduler=MockScheduler(), workflow=workflow)
    deploying = make_service_context(make_inference_service(phase=InferencePhase.DEPLOYING))
    verifying = make_service_context(make_inference_service(phase=InferencePhase.VERIFYING))

    to_verifying = asyncio.run(controller.reconcile(deploying))
    ready = asyncio.run(controller.reconcile(verifying))
    replay = asyncio.run(controller.reconcile(verifying))

    assert (to_verifying.status_patch["phase"], to_verifying.requeue_after) == ("Verifying", 0.0)
    assert (ready.done, ready.requeue_after) == (True, None)
    assert ready.status_patch == {
        "phase": "Ready",
        "endpoints": [
            {
                "url": "http://10.0.0.5:8000",
                "ready": True,
                "instanceId": "instance-1",
                "protocol": "openai-chat",
            }
        ],
        "conditions": [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Reconciled",
                "message": "instance instance-1 is serving at http://10.0.0.5:8000",
            }
        ],
    }
    assert verifying.dedup_key in controller._processed_keys
    assert (replay.done, replay.status_patch) == (True, {})
    assert len(workflow.lookups) == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("run", "message"),
    [
        (
            make_run(
                WorkflowStatus.FAILED,
                StepStatus.COMPENSATED,
                error="step 'verify-instance' failed: connection refused",
            ),
            "step 'verify-instance' failed: connection refused",
        ),
        (
            make_run(
                WorkflowStatus.FAILED,
                StepStatus.FAILED,
                error="step 'launch-instance' failed: image not found",
            ),
            "step 'launch-instance' failed: image not found",
        ),
        (
            make_run(WorkflowStatus.CANCELLED, StepStatus.CANCELLED),
            "deployment workflow run run-1 ended Cancelled",
        ),
        (
            make_run(WorkflowStatus.TIMEDOUT, StepStatus.SKIPPED),
            "deployment workflow run run-1 ended TimedOut",
        ),
    ],
)
def test_a_failed_run_fails_the_service_and_releases_its_allocation(run, message):
    """运行失败（补偿已停止实例）、被取消或超时：进入终态 Failed（登记去重键），
    Ready=False（WorkflowFailed，消息含失败的步骤），释放分配。"""
    registry, service = _allocated_service(InferencePhase.VERIFYING)
    controller = _controller(
        registry=registry, scheduler=MockScheduler(), workflow=StubWorkflowRuntime(run=run)
    )
    ctx = make_service_context(service)

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (True, None)
    assert result.status_patch == {"phase": "Failed", **_not_ready("WorkflowFailed", message)}
    assert ctx.dedup_key in controller._processed_keys
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is None
    assert asyncio.run(registry.allocated_devices()) == frozenset()


@pytest.mark.unit
def test_an_instance_that_compensation_could_not_stop_keeps_the_allocation_for_now():
    """补偿没能停止实例：进入 Failed 但暂时保留分配（实例可能仍在运行），立即重新排队，由
    之后的 Failed 一轮停止实例、释放分配。"""
    run = make_run(
        WorkflowStatus.FAILED,
        StepStatus.COMPENSATION_FAILED,
        error="step 'verify-instance' failed: timed out",
    )
    registry, service = _allocated_service(InferencePhase.VERIFYING)
    controller = _controller(
        registry=registry, scheduler=MockScheduler(), workflow=StubWorkflowRuntime(run=run)
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert (result.done, result.requeue_after) == (True, 0.0)
    assert result.status_patch == {
        "phase": "Failed",
        **_not_ready(
            "WorkflowFailed",
            "step 'verify-instance' failed: timed out; instance instance-1 may still be running "
            "and is stopped before the allocation is released",
        ),
    }
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("run", "reason", "message"),
    [
        (None, "WorkflowRunNotFound", "deployment workflow run 'deployment:uid-1:1' not found"),
        (
            make_run(WorkflowStatus.SUCCEEDED, outputs=False),
            "WorkflowFailed",
            "deployment workflow run run-1 succeeded without reporting an instance",
        ),
    ],
)
def test_a_run_that_cannot_tell_where_its_instance_is_keeps_the_allocation(run, reason, message):
    """找不到运行（如工作流数据库被重置），或运行成功却没有报告实例：无法确认实例是否
    在运行，进入 Failed 并保留分配，立即重新排队（之后的 Failed 一轮同样保留分配）。"""
    registry, service = _allocated_service(InferencePhase.DEPLOYING)
    controller = _controller(
        registry=registry, scheduler=MockScheduler(), workflow=StubWorkflowRuntime(run=run)
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert (result.done, result.requeue_after, result.status_patch) == (
        True,
        0.0,
        {"phase": "Failed", **_not_ready(reason, message)},
    )
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is not None


@pytest.mark.unit
def test_a_failed_service_stops_its_instance_before_releasing_its_allocation():
    """spec 没有变化的 Failed 一轮（补偿没能停止实例，分配还在）：经 release provider 停止
    实例，然后释放分配；这一轮不写 status、登记去重键，之后的协调不再停止。"""
    run = make_run(WorkflowStatus.FAILED, StepStatus.COMPENSATION_FAILED)
    registry, service = _allocated_service(InferencePhase.FAILED, observed_generation=1)
    release = StubReleaseProvider()
    controller = _controller(
        registry=registry, workflow=StubWorkflowRuntime(run=run), release=release
    )
    ctx = make_service_context(service)

    results = [asyncio.run(controller.reconcile(ctx)) for _ in range(2)]

    assert [(r.done, r.requeue_after, r.status_patch) for r in results] == [(True, None, {})] * 2
    assert release.released == ["svc-1"]
    assert ctx.dedup_key in controller._processed_keys
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is None
    assert asyncio.run(registry.allocated_devices()) == frozenset()


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["lookup", "stop"])
def test_a_failed_service_whose_deployment_cannot_be_stopped_keeps_its_allocation(failure):
    """Failed 一轮查询运行或停止实例失败：保留分配，不写 status、不登记去重键，按指数退避
    重试；之后停止成功时释放分配。"""
    workflow = StubWorkflowRuntime(
        run=make_run(WorkflowStatus.FAILED, StepStatus.COMPENSATION_FAILED)
    )
    release = StubReleaseProvider()
    if failure == "lookup":
        workflow.lookup_error = RuntimeError("database is locked")
    else:
        release.fail = True
    registry, service = _allocated_service(InferencePhase.FAILED, observed_generation=1)
    controller = _controller(
        registry=registry, workflow=workflow, release=release, workflow_poll_interval=0.5
    )
    ctx = make_service_context(service)

    failures = [asyncio.run(controller.reconcile(ctx)) for _ in range(2)]
    held = asyncio.run(registry.get_active_allocation("uid-1", "tenant-1"))
    marked = ctx.dedup_key in controller._processed_keys
    workflow.lookup_error, release.fail = None, False
    stopped = asyncio.run(controller.reconcile(ctx))

    assert [(r.done, r.requeue_after, r.status_patch) for r in failures] == [
        (False, 0.5, {}),
        (False, 1.0, {}),
    ]
    assert (held is not None, marked) == (True, False)
    assert (stopped.done, stopped.status_patch) == (True, {})
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "runs",
    [
        {},
        {RUN_KEY: make_run(WorkflowStatus.SUCCEEDED, outputs=False)},
        {
            RUN_KEY: make_run(WorkflowStatus.SUCCEEDED, outputs=False),
            deployment_run_key("uid-1", 2): make_run(
                WorkflowStatus.FAILED, StepStatus.COMPENSATION_FAILED
            ),
        },
    ],
    ids=["no-run", "no-instance-reported", "one-run-reports-no-instance"],
)
def test_a_failed_service_whose_instance_cannot_be_found_keeps_its_allocation(runs):
    """Failed 一轮找不到运行，或有运行成功却没有报告实例：无从停止实例，不调用 release
    provider，保留分配（删除资源时释放），登记去重键。"""
    registry, service = _allocated_service(InferencePhase.FAILED, observed_generation=1)
    workflow = StubWorkflowRuntime()
    workflow.runs = dict(runs)
    release = StubReleaseProvider()
    controller = _controller(registry=registry, workflow=workflow, release=release)
    ctx = make_service_context(service)

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after, result.status_patch) == (True, None, {})
    assert release.released == []
    assert ctx.dedup_key in controller._processed_keys
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is not None


@pytest.mark.unit
def test_a_failed_service_without_an_allocation_has_nothing_to_stop():
    """Failed 一轮且资源没有 Active 分配（失败时已释放）：不查询运行、不调用 release
    provider，登记去重键。"""
    registry = SQLiteRegistry()
    service = make_inference_service(phase=InferencePhase.FAILED, observed_generation=1)
    asyncio.run(registry.put(service))
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.FAILED, StepStatus.COMPENSATED))
    release = StubReleaseProvider()
    controller = _controller(registry=registry, workflow=workflow, release=release)
    ctx = make_service_context(service)

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.status_patch) == (True, {})
    assert (workflow.lookups, release.released) == ([], [])
    assert ctx.dedup_key in controller._processed_keys


@pytest.mark.unit
def test_a_failed_run_lookup_keeps_the_phase_and_backs_off():
    """查询运行失败：停在当前 phase、Ready=False（WorkflowLookupFailed），不登记去重键，
    按指数退避重试；查询成功后退避从头开始。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.RUNNING, StepStatus.RUNNING))
    workflow.lookup_error = RuntimeError("database is locked")
    controller = _controller(
        scheduler=MockScheduler(), workflow=workflow, workflow_poll_interval=0.5
    )
    ctx = make_service_context(make_inference_service(phase=InferencePhase.DEPLOYING))

    failures = [asyncio.run(controller.reconcile(ctx)) for _ in range(2)]
    workflow.lookup_error = None
    polled = asyncio.run(controller.reconcile(ctx))
    workflow.lookup_error = RuntimeError("database is locked")
    failed_again = asyncio.run(controller.reconcile(ctx))

    assert [result.requeue_after for result in failures] == [0.5, 1.0]
    assert failures[0].status_patch == _not_ready("WorkflowLookupFailed", "database is locked")
    assert (polled.requeue_after, polled.status_patch) == (0.5, {})
    assert failed_again.requeue_after == 0.5
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
@pytest.mark.parametrize(
    ("phase", "observed_generation"),
    [
        (InferencePhase.READY, 2),
        (InferencePhase.FAILED, 2),
        (InferencePhase.UNKNOWN, 1),
    ],
)
def test_services_outside_a_deployment_are_left_alone(phase, observed_generation):
    """部署之外没有可做的事：spec 没有变化的 Ready / Failed（不自动重试），以及 Unknown
    （spec 变化也一样）。"""
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime()
    controller = _controller(scheduler=scheduler, workflow=workflow)
    service = make_inference_service(
        phase=phase, generation=2, observed_generation=observed_generation
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert (result.done, result.requeue_after, result.status_patch) == (True, None, {})
    assert scheduler.allocate_called is False
    assert (workflow.started, workflow.lookups) == ([], [])


@pytest.mark.unit
def test_a_failed_service_whose_spec_changed_goes_back_to_pending():
    """Failed 且 spec 变化：回到 Pending、清空 endpoint，立即重新排队；这一轮没有副作用，
    不评估策略（Pending 一轮像对新建的资源一样授权），不登记去重键。"""
    policy = SequencePolicyEngine("deny")
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.FAILED))
    release = StubReleaseProvider()
    controller = _controller(policy=policy, scheduler=scheduler, workflow=workflow, release=release)
    service = make_inference_service(
        phase=InferencePhase.FAILED, generation=2, observed_generation=1
    )
    ctx = make_service_context(service)

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (False, 0.0)
    assert result.status_patch == {
        "phase": "Pending",
        "endpoints": [],
        **_not_ready("Pending", "the spec changed (generation 2); deploying it again"),
    }
    assert (policy.calls, scheduler.requests, workflow.lookups, release.released) == (
        0,
        [],
        [],
        [],
    )
    assert ctx.dedup_key not in controller._processed_keys


# ---------------------------------------------------------------------- #
# 部署过程中 spec 变化
# ---------------------------------------------------------------------- #


def _two_gpu_scheduler(registry: SQLiteRegistry) -> FirstFitScheduler:
    """``node-a`` 上一张 24 GiB（``gpu-0``）与一张 80 GiB（``gpu-1``）的卡。"""
    devices = [
        {"id": "gpu-0", "type": "nvidia-gpu", "memoryGiB": 24},
        {"id": "gpu-1", "type": "nvidia-gpu", "memoryGiB": 80},
    ]
    inventory = Inventory.model_validate_json(
        json.dumps({"nodes": [{"id": "node-a", "devices": devices}]})
    )
    return FirstFitScheduler(registry, inventory)


@pytest.mark.unit
def test_preparing_stops_the_previous_deployment_before_starting_a_run():
    """Preparing 没有当前 generation 的运行：先经 release provider 停止资源之前的部署（如
    旧 generation 仍在进行的运行），再启动当前 generation 的运行。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.RUNNING, StepStatus.RUNNING))
    release = SnapshotReleaseProvider(workflow)
    controller = _controller(scheduler=MockScheduler(), workflow=workflow, release=release)
    service = make_inference_service(phase=InferencePhase.PREPARING, generation=2)

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert release.started_before == [[]]
    assert workflow.idempotency_keys == [deployment_run_key("uid-1", 2)]
    assert result.status_patch == _progress("Deploying", "deployment workflow run run-1 started")


@pytest.mark.unit
def test_a_replayed_preparing_pass_follows_the_run_it_already_started():
    """上一轮启动了当前 generation 的运行、没能写入 Deploying：重放的一轮沿用这次运行，
    不停止它、也不再启动。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.RUNNING, StepStatus.RUNNING))
    release = StubReleaseProvider()
    controller = _controller(scheduler=MockScheduler(), workflow=workflow, release=release)
    ctx = make_service_context(make_inference_service(phase=InferencePhase.PREPARING))

    result = asyncio.run(controller.reconcile(ctx))

    assert (release.released, workflow.started) == ([], [])
    assert result.status_patch == _progress("Deploying", "deployment workflow run run-1 started")


@pytest.mark.unit
@pytest.mark.parametrize("phase", [InferencePhase.DEPLOYING, InferencePhase.VERIFYING])
@pytest.mark.parametrize(
    "run",
    [make_run(WorkflowStatus.RUNNING, StepStatus.SUCCEEDED), make_run(WorkflowStatus.SUCCEEDED)],
    ids=["in-progress", "succeeded"],
)
def test_a_spec_change_during_the_deployment_deploys_the_new_generation(phase, run):
    """部署过程中 spec 变化（只有上一代 spec 的运行）：停止之前的部署，以新的 generation
    取得分配、启动运行，phase 不变；上一代的运行即使已成功也不会让服务进入 Ready。"""
    workflow = StubWorkflowRuntime(run=run)
    release = SnapshotReleaseProvider(workflow)
    scheduler = MockScheduler()
    controller = _controller(
        scheduler=scheduler, workflow=workflow, release=release, workflow_poll_interval=0.5
    )
    service = make_inference_service(phase=phase, generation=2, observed_generation=1)

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    [(_, inputs, key)] = workflow.started
    assert key == deployment_run_key("uid-1", 2)
    assert LaunchRequest.model_validate(inputs["request"]).instance_key == key
    assert release.started_before == [[]]
    assert [req.idempotency_key for req in scheduler.requests] == ["uid-1:2"]
    assert (result.done, result.requeue_after) == (False, 0.5)
    assert result.status_patch == _not_ready(
        phase.value,
        "generation 2 replaces the deployment in progress: deployment workflow run run-1 started",
    )


@pytest.mark.unit
def test_a_previous_deployment_that_cannot_be_stopped_blocks_the_new_one():
    """停止之前的部署失败：不分配、不启动新的运行，停在当前 phase（StopFailed），按指数退避
    重试，不登记去重键。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.RUNNING, StepStatus.SUCCEEDED))
    scheduler = MockScheduler()
    controller = _controller(
        scheduler=scheduler,
        workflow=workflow,
        release=StubReleaseProvider(fail=True),
        workflow_poll_interval=0.5,
    )
    service = make_inference_service(phase=InferencePhase.VERIFYING, generation=2)
    ctx = make_service_context(service)

    results = [asyncio.run(controller.reconcile(ctx)) for _ in range(2)]

    assert [result.requeue_after for result in results] == [0.5, 1.0]
    assert results[0].status_patch == _not_ready(
        "StopFailed",
        "the previous deployment could not be stopped: release failed: gpu driver error",
    )
    assert (scheduler.requests, workflow.started) == ([], [])
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
@pytest.mark.parametrize("phase", [InferencePhase.DEPLOYING, InferencePhase.UPDATING])
def test_replacing_a_deployment_without_a_scheduler_reports_unavailable(phase):
    """需要为新的 generation 取得分配、却未注入调度器：SchedulerUnavailable，不停止之前的
    部署、不启动运行。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.RUNNING, StepStatus.RUNNING))
    release = StubReleaseProvider()
    controller = _controller(workflow=workflow, release=release)
    service = make_inference_service(phase=phase, generation=2)

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert (result.done, result.requeue_after) == (False, None)
    assert result.status_patch == _not_ready(
        "SchedulerUnavailable", "no scheduler is configured to allocate resources"
    )
    assert (release.released, workflow.started) == ([], [])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("phase", "next_phase"),
    [
        (InferencePhase.SCHEDULING, "Preparing"),
        (InferencePhase.PREPARING, "Deploying"),
        (InferencePhase.VERIFYING, None),
    ],
)
def test_an_accelerator_change_releases_the_allocation_and_allocates_again(phase, next_phase):
    """资源持有的分配（第 1 代 spec 的 24 GiB 卡）不满足新的申请（至少 40 GiB）：调度器拒绝
    复用，协调器先停止之前的部署、释放这个分配，再按新的申请分配（80 GiB 卡）并照常推进。"""
    registry = SQLiteRegistry()
    accelerator = AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=40)
    service = make_inference_service(
        phase=phase, accelerator=accelerator, generation=2, observed_generation=1
    )
    asyncio.run(registry.put(service))
    previous = _allocate(registry, service)
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.RUNNING, StepStatus.SUCCEEDED))
    release = SnapshotReleaseProvider(workflow)
    controller = _controller(
        registry=registry,
        scheduler=_two_gpu_scheduler(registry),
        workflow=workflow,
        release=release,
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    active = asyncio.run(registry.get_active_allocation("uid-1", "tenant-1"))
    assert asyncio.run(registry.get_allocation(previous["id"]))["status"] == "Released"
    assert (active["idempotency_key"], active["devices"]) == (
        "uid-1:2",
        [{"node_id": "node-a", "device_id": "gpu-1"}],
    )
    assert release.started_before[0] == []
    assert result.status_patch.get("phase") == next_phase
    assert result.status_patch["conditions"][0]["reason"] != "AllocationFailed"


@pytest.mark.unit
def test_an_accelerator_request_that_no_longer_fits_releases_the_allocation_and_waits():
    """新的申请放不下：之前的部署已停止、分配已释放，停在当前 phase（AllocationFailed），
    10 秒后再试。"""
    registry = SQLiteRegistry()
    accelerator = AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=100)
    service = make_inference_service(
        phase=InferencePhase.SCHEDULING, accelerator=accelerator, generation=2
    )
    asyncio.run(registry.put(service))
    previous = _allocate(registry, service)
    release = StubReleaseProvider()
    controller = _controller(
        registry=registry,
        scheduler=_two_gpu_scheduler(registry),
        workflow=StubWorkflowRuntime(),
        release=release,
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert (result.done, result.requeue_after) == (False, 10.0)
    assert result.status_patch == _not_ready(
        "AllocationFailed", "no node has 1 free nvidia-gpu device(s) with at least 100 GiB"
    )
    assert release.released == ["svc-1"]
    assert asyncio.run(registry.get_allocation(previous["id"]))["status"] == "Released"
    assert asyncio.run(registry.allocated_devices()) == frozenset()


@pytest.mark.unit
def test_an_allocation_conflict_without_a_previous_allocation_is_an_allocation_failure():
    """调度器报告冲突、资源却没有为之前的 generation 持有分配（持有的就是当前幂等键的
    分配）：AllocationFailed，不停止部署、不释放分配。"""

    class ConflictingScheduler(MockScheduler):
        async def allocate(self, req: ScheduleRequest) -> AllocationRef:
            await super().allocate(req)
            raise ConflictError("idempotency_key 'uid-1:1' belongs to another resource")

    registry, service = _allocated_service(InferencePhase.SCHEDULING)
    release = StubReleaseProvider()
    controller = _controller(
        registry=registry,
        scheduler=ConflictingScheduler(),
        workflow=StubWorkflowRuntime(),
        release=release,
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert (result.done, result.requeue_after) == (False, 10.0)
    assert result.status_patch == _not_ready(
        "AllocationFailed", "idempotency_key 'uid-1:1' belongs to another resource"
    )
    assert release.released == []
    assert asyncio.run(registry.get_active_allocation("uid-1", "tenant-1")) is not None


# ---------------------------------------------------------------------- #
# 更新已部署的服务：Ready / Degraded → Updating → Ready
# ---------------------------------------------------------------------- #


def _changed_service(phase: InferencePhase = InferencePhase.READY) -> InferenceService:
    """spec 已变化（第 2 代，已处理到第 1 代）、实例为 :data:`INSTANCE` 的服务。"""
    service = make_inference_service(phase=phase, generation=2, observed_generation=1)
    ready = phase == InferencePhase.READY
    service.status.endpoints = [INSTANCE.endpoint().model_copy(update={"ready": ready})]
    return service


@pytest.mark.unit
@pytest.mark.parametrize("phase", [InferencePhase.READY, InferencePhase.DEGRADED])
def test_a_spec_change_moves_a_serving_service_to_updating(phase):
    """spec 变化：进入 Updating、endpoint 标记为未就绪，立即重新排队；这一轮没有副作用，
    不登记去重键。"""
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    release = StubReleaseProvider()
    controller = _controller(scheduler=scheduler, workflow=workflow, release=release)
    ctx = make_service_context(_changed_service(phase))

    result = asyncio.run(controller.reconcile(ctx))

    endpoint = INSTANCE.endpoint().model_copy(update={"ready": False})
    assert (result.done, result.requeue_after) == (False, 0.0)
    assert result.status_patch == {
        "phase": "Updating",
        "endpoints": [endpoint.model_dump(mode="json")],
        **_not_ready("Updating", "deploying generation 2"),
    }
    assert (scheduler.requests, workflow.lookups, workflow.started) == ([], [], [])
    assert release.released == []
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
@pytest.mark.parametrize(
    ("decision", "reason"),
    [("deny", "PolicyDenied"), ("require_approval", "PolicyApprovalRequired")],
)
def test_an_update_the_policy_blocks_writes_no_status(decision, reason, caplog):
    """策略拒绝或要求审批：服务照旧，不写 status（observedGeneration 不变，更新仍待处理），
    日志说明原因，30 秒后重新评估。"""
    policy = SequencePolicyEngine(decision, "allow")
    controller = _controller(
        policy=policy, scheduler=MockScheduler(), workflow=StubWorkflowRuntime()
    )
    ctx = make_service_context(_changed_service())

    with caplog.at_level(logging.WARNING):
        blocked = asyncio.run(controller.reconcile(ctx))
    allowed = asyncio.run(controller.reconcile(ctx))

    assert (blocked.done, blocked.requeue_after, blocked.status_patch) == (False, 30.0, {})
    assert f"update not started: reason={reason} message=call-1" in caplog.text
    assert allowed.status_patch["phase"] == "Updating"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("components", "reason"),
    [
        ({"workflow": StubWorkflowRuntime()}, "SchedulerUnavailable"),
        ({"scheduler": MockScheduler()}, "WorkflowRuntimeUnavailable"),
    ],
    ids=["without-scheduler", "without-workflow-runtime"],
)
def test_an_update_that_cannot_be_deployed_writes_no_status(components, reason, caplog):
    """缺少调度器或工作流运行时：不进入 Updating、不写 status，服务照旧；配置变化需要重启，
    重启后的全量同步再处理这次更新。"""
    controller = _controller(**components)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(controller.reconcile(make_service_context(_changed_service())))

    assert (result.done, result.requeue_after, result.status_patch) == (False, None, {})
    assert f"update not started: reason={reason}" in caplog.text


@pytest.mark.unit
def test_updating_stops_the_previous_deployment_and_deploys_the_new_generation():
    """Updating 且没有新 generation 的运行：先停止之前的部署，再以新 generation 的幂等键
    取得分配、启动运行，清空 status.endpoints，按 workflow_poll_interval 重新排队。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    release = SnapshotReleaseProvider(workflow)
    scheduler = MockScheduler()
    controller = _controller(
        scheduler=scheduler, workflow=workflow, release=release, workflow_poll_interval=0.5
    )
    service = make_inference_service(
        phase=InferencePhase.UPDATING, generation=2, observed_generation=2
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    [(_, inputs, key)] = workflow.started
    assert release.started_before == [[]]
    assert [req.idempotency_key for req in scheduler.requests] == ["uid-1:2"]
    assert key == deployment_run_key("uid-1", 2)
    assert LaunchRequest.model_validate(inputs["request"]).spec == service.spec
    assert (result.done, result.requeue_after) == (False, 0.5)
    assert result.status_patch == {
        "endpoints": [],
        **_not_ready("Updating", "deployment workflow run run-1 deploys generation 2"),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("run", "phase"),
    [
        (make_run(WorkflowStatus.RUNNING, StepStatus.SUCCEEDED), None),
        (make_run(WorkflowStatus.SUCCEEDED), "Ready"),
        (
            make_run(
                WorkflowStatus.FAILED,
                StepStatus.COMPENSATED,
                error="step 'verify-instance' failed: connection refused",
            ),
            "Failed",
        ),
    ],
    ids=["in-progress", "succeeded", "failed"],
)
def test_updating_follows_the_run_of_the_new_generation(run, phase):
    """新 generation 的运行进行中时轮询、不写 status；成功后进入 Ready（写入新实例的地址并
    登记去重键），失败后进入 Failed。不再停止部署、不再启动运行。"""
    workflow = StubWorkflowRuntime(run=run, key=deployment_run_key("uid-1", 2))
    release = StubReleaseProvider()
    controller = _controller(scheduler=MockScheduler(), workflow=workflow, release=release)
    service = make_inference_service(
        phase=InferencePhase.UPDATING, generation=2, observed_generation=2
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert (result.done, result.status_patch.get("phase")) == (phase is not None, phase)
    assert (release.released, workflow.started) == ([], [])
    if phase == "Ready":
        assert result.status_patch["endpoints"] == [INSTANCE.endpoint().model_dump(mode="json")]


# ---------------------------------------------------------------------- #
# 实例丢失后重新部署（Degraded，以及运行成功之后实例丢失的 Verifying / Updating）
# ---------------------------------------------------------------------- #


class StubLauncher:
    """只回答 ``is_running`` 的启动器替身：``running`` 中的实例在运行。"""

    def __init__(self, *running: str) -> None:
        self.running = set(running)

    async def is_running(self, instance_id: str) -> bool:
        return instance_id in self.running


class ManualClock:
    """手动推进的单调时钟（秒）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _degraded_service() -> InferenceService:
    """spec 没有变化（第 1 代）、实例 :data:`INSTANCE` 已标记为未就绪的 Degraded 服务。"""
    service = make_inference_service(phase=InferencePhase.DEGRADED, observed_generation=1)
    service.status.endpoints = [INSTANCE.endpoint().model_copy(update={"ready": False})]
    return service


def _redeploying(attempt: int) -> dict[str, Any]:
    message = f"deployment workflow run run-1 redeploys the service (attempt {attempt})"
    return {"endpoints": [], **_not_ready("InstanceLost", message)}


@pytest.mark.unit
@pytest.mark.parametrize(
    "launcher", [StubLauncher("instance-1"), None], ids=["instance-back", "without-launcher"]
)
def test_a_degraded_service_whose_instance_runs_returns_to_ready(launcher):
    """最近一次部署的运行成功、实例在运行（未注入启动器时无从确认，视为在运行）：回到
    Ready，写入实例地址；不停止、不启动部署，也不登记去重键（同一代 spec 可能再次丢失实例）。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    release = StubReleaseProvider()
    controller = _controller(
        scheduler=MockScheduler(), workflow=workflow, release=release, launcher=launcher
    )
    ctx = make_service_context(_degraded_service())

    result = asyncio.run(controller.reconcile(ctx))

    assert (result.done, result.requeue_after) == (False, None)
    assert result.status_patch["phase"] == "Ready"
    assert result.status_patch["endpoints"] == [INSTANCE.endpoint().model_dump(mode="json")]
    assert (release.released, workflow.started) == ([], [])
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_a_degraded_service_whose_instance_is_gone_is_deployed_again():
    """实例不在运行：不评估策略（这一代 spec 已经授权），先停止之前的部署，再以这一代 spec
    的分配幂等键取得分配、以下一次部署的幂等键启动运行；清空 status.endpoints，phase 不变，
    不登记去重键。"""
    policy = SequencePolicyEngine("deny")
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    release = SnapshotReleaseProvider(workflow)
    scheduler = MockScheduler()
    controller = _controller(
        policy=policy,
        scheduler=scheduler,
        workflow=workflow,
        release=release,
        launcher=StubLauncher(),
        workflow_poll_interval=0.5,
    )
    ctx = make_service_context(_degraded_service())

    result = asyncio.run(controller.reconcile(ctx))

    [(_, inputs, key)] = workflow.started
    assert key == deployment_run_key("uid-1", 1, 2)
    assert LaunchRequest.model_validate(inputs["request"]).instance_key == key
    assert release.started_before == [[]]
    assert [req.idempotency_key for req in scheduler.requests] == ["uid-1:1"]
    assert policy.calls == 0
    assert (result.done, result.requeue_after) == (False, 0.5)
    assert result.status_patch == _redeploying(2)
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_a_redeployment_in_progress_is_polled_without_writing_status():
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    workflow.runs[deployment_run_key("uid-1", 1, 2)] = make_run(
        WorkflowStatus.RUNNING, StepStatus.RUNNING
    )
    release = StubReleaseProvider()
    controller = _controller(
        scheduler=MockScheduler(),
        workflow=workflow,
        release=release,
        launcher=StubLauncher(),
        workflow_poll_interval=0.5,
    )

    result = asyncio.run(controller.reconcile(make_service_context(_degraded_service())))

    assert (result.done, result.requeue_after, result.status_patch) == (False, 0.5, {})
    assert (release.released, workflow.started) == ([], [])


@pytest.mark.unit
def test_a_degraded_service_deployed_before_runs_were_keyed_by_generation_is_deployed_again():
    """运行来自 1.1.0 之前（幂等键 ``deployment:{uid}``，不含 generation）：这一代 spec 没有
    运行，以它的第一次部署重新部署。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED), key="deployment:uid-1")
    controller = _controller(scheduler=MockScheduler(), workflow=workflow, launcher=StubLauncher())

    result = asyncio.run(controller.reconcile(make_service_context(_degraded_service())))

    assert workflow.idempotency_keys == [RUN_KEY]
    assert result.status_patch == _redeploying(1)


@pytest.mark.unit
def test_redeployments_of_a_generation_back_off_from_the_last_start():
    """第一次重新部署立即开始；之后等到与上一次开始相隔 10 秒、再 20 秒……，等待的一轮没有
    副作用、不写 status；新的 generation 从头开始。"""
    clock = ManualClock()
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    controller = _controller(
        scheduler=MockScheduler(), workflow=workflow, launcher=StubLauncher(), clock=clock
    )
    ctx = make_service_context(_degraded_service())

    first = asyncio.run(controller.reconcile(ctx))
    waiting = asyncio.run(controller.reconcile(ctx))
    clock.now = 9.0
    still_waiting = asyncio.run(controller.reconcile(ctx))
    clock.now = 10.0
    second = asyncio.run(controller.reconcile(ctx))
    waiting_longer = asyncio.run(controller.reconcile(ctx))
    newer = make_inference_service(
        phase=InferencePhase.DEGRADED, generation=2, observed_generation=2
    )
    workflow.runs[deployment_run_key("uid-1", 2)] = make_run(WorkflowStatus.SUCCEEDED)
    next_generation = asyncio.run(controller.reconcile(make_service_context(newer)))

    assert (first.status_patch, second.status_patch) == (_redeploying(2), _redeploying(3))
    assert [(r.done, r.requeue_after, r.status_patch) for r in (waiting, still_waiting)] == [
        (False, REDEPLOY_BACKOFF_INITIAL_SECONDS, {}),
        (False, 1.0, {}),
    ]
    assert waiting_longer.requeue_after == 2 * REDEPLOY_BACKOFF_INITIAL_SECONDS
    assert next_generation.status_patch == _redeploying(2)
    assert workflow.idempotency_keys == [
        deployment_run_key("uid-1", 1, 2),
        deployment_run_key("uid-1", 1, 3),
        deployment_run_key("uid-1", 2, 2),
    ]


@pytest.mark.unit
def test_a_failed_redeployment_reports_why_while_the_service_waits():
    """重新部署的运行失败：等待退避期间不启动运行，Ready=False（WorkflowFailed）说明失败原因。"""
    clock = ManualClock()
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    controller = _controller(
        scheduler=MockScheduler(), workflow=workflow, launcher=StubLauncher(), clock=clock
    )
    ctx = make_service_context(_degraded_service())
    asyncio.run(controller.reconcile(ctx))
    workflow.runs[deployment_run_key("uid-1", 1, 2)] = make_run(
        WorkflowStatus.FAILED,
        StepStatus.COMPENSATED,
        error="step 'verify-instance' failed: connection refused",
    )

    waiting = asyncio.run(controller.reconcile(ctx))

    assert (waiting.done, waiting.requeue_after) == (False, REDEPLOY_BACKOFF_INITIAL_SECONDS)
    assert waiting.status_patch == _not_ready(
        "WorkflowFailed",
        "step 'verify-instance' failed: connection refused; redeploying the service after a "
        "backoff",
    )
    assert len(workflow.started) == 1


@pytest.mark.unit
def test_a_redeployment_that_cannot_stop_the_previous_deployment_backs_off():
    """停止之前的部署失败：不启动运行，Ready=False（StopFailed），按指数退避重试；每轮查询
    运行成功不会让退避从头开始。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    controller = _controller(
        scheduler=MockScheduler(),
        workflow=workflow,
        release=StubReleaseProvider(fail=True),
        launcher=StubLauncher(),
        workflow_poll_interval=0.5,
    )
    ctx = make_service_context(_degraded_service())

    results = [asyncio.run(controller.reconcile(ctx)) for _ in range(3)]

    assert [result.requeue_after for result in results] == [0.5, 1.0, 2.0]
    assert results[0].status_patch == _not_ready(
        "StopFailed",
        "the previous deployment could not be stopped: release failed: gpu driver error",
    )
    assert workflow.started == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("phase", "generation"), [(InferencePhase.VERIFYING, 1), (InferencePhase.UPDATING, 2)]
)
def test_an_instance_lost_before_the_service_is_ready_is_deployed_again(phase, generation):
    """运行成功、写入 Ready 之前实例已不在运行（如控制平面恰好重启）：phase 不变，以同一代
    spec 的下一次部署重新部署，不把不存在的实例写进 status.endpoints。"""
    workflow = StubWorkflowRuntime(
        run=make_run(WorkflowStatus.SUCCEEDED), key=deployment_run_key("uid-1", generation)
    )
    controller = _controller(scheduler=MockScheduler(), workflow=workflow, launcher=StubLauncher())
    service = make_inference_service(
        phase=phase, generation=generation, observed_generation=generation
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert workflow.idempotency_keys == [deployment_run_key("uid-1", generation, 2)]
    assert (result.done, result.status_patch) == (False, _redeploying(2))


@pytest.mark.unit
def test_a_degraded_service_whose_run_reported_no_instance_fails():
    """最近一次部署的运行成功却没有报告实例（运行时不返回步骤输出）：与部署中一样进入
    Failed、保留分配，不重新部署（同样无从得知、无从停止新的实例）。"""
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED, outputs=False))
    controller = _controller(scheduler=MockScheduler(), workflow=workflow, launcher=StubLauncher())

    result = asyncio.run(controller.reconcile(make_service_context(_degraded_service())))

    assert (result.done, result.status_patch) == (
        True,
        {
            "phase": "Failed",
            **_not_ready(
                "WorkflowFailed",
                "deployment workflow run run-1 succeeded without reporting an instance",
            ),
        },
    )
    assert workflow.started == []


# ---------------------------------------------------------------------- #
# 幂等与去重键登记
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_reconcile_idempotent():
    """部署完成（Ready）时登记去重键：同一 dedup_key 的重放直接返回 done=True，无副作用。"""
    scheduler = MockScheduler()
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED), key=None)
    controller = _controller(scheduler=scheduler, workflow=workflow)
    asyncio.run(_deploy(controller, make_inference_service(phase=InferencePhase.PENDING)))

    pending = make_service_context(make_inference_service(phase=InferencePhase.PENDING))
    replay = asyncio.run(controller.reconcile(pending))

    assert (replay.done, replay.requeue_after, replay.status_patch) == (True, None, {})
    assert scheduler.allocate_count == 2
    assert workflow.idempotency_keys == [RUN_KEY]


@pytest.mark.unit
@pytest.mark.parametrize("make_store", [lambda: None, lambda: ProcessedKeyStore(":memory:")])
def test_caller_records_dedup_keys_when_reconciler_does_not(make_store):
    """record_dedup_keys=False：reconcile 只检查去重键，由调用方在写入 status 后登记。"""
    ctx = make_service_context(make_inference_service(phase=InferencePhase.VERIFYING))
    workflow = StubWorkflowRuntime(run=make_run(WorkflowStatus.SUCCEEDED))
    store = make_store()
    controller = _controller(
        scheduler=MockScheduler(),
        workflow=workflow,
        processed_store=store,
        record_dedup_keys=False,
    )

    first = asyncio.run(controller.reconcile(ctx))
    assert first.done is True
    assert controller.records_dedup_keys is False
    assert ctx.dedup_key not in controller._processed_keys
    if store is not None:
        assert asyncio.run(store.seen(ctx.dedup_key)) is False

    asyncio.run(controller.mark_processed(ctx.dedup_key))
    again = asyncio.run(controller.reconcile(ctx))
    assert again.status_patch == {}
    assert len(workflow.lookups) == 1


# ---------------------------------------------------------------------- #
# Terminating 受控删除闭环（architecture §5.3/§12.4 / development-plan 第 7 章）
# ---------------------------------------------------------------------- #


def _terminating_context(
    registry: SQLiteRegistry,
    name: str = "svc-1",
    tenant_id: str = "tenant-1",
    dedup_key: str = "term-1",
    namespace: str | None = None,
) -> ReconcileContext:
    """构造 Terminating 资源的协调上下文（resource 以注册表读回为准注入）。"""
    resource = asyncio.run(registry.get("InferenceService", namespace, name, tenant_id))
    return ReconcileContext(
        resource=resource,
        observed={"phase": "Terminating"},
        correlation_id="corr-term",
        tenant_id=tenant_id,
        dedup_key=dedup_key,
    )


def _make_controller(
    registry: SQLiteRegistry,
    release_provider: Any | None = None,
) -> ReconciliationController:
    return ReconciliationController(
        registry=registry,
        policy_engine=MockPolicyEngine(ALLOW),
        release_provider=release_provider,
    )


@pytest.mark.unit
def test_terminating_full_delete_closure():
    """删除闭环：Ready → DELETE(Terminating) → reconcile 释放 → finalizer 清空
    → Allocation Released（设备回到空闲）→ 物理删除。"""
    reg = SQLiteRegistry()
    service = make_inference_service()
    asyncio.run(reg.put(service))
    _allocate(reg, service)
    # DELETE 受理：Ready 有副作用 → 自动加 finalizer → Terminating
    delete_result = asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))
    assert delete_result.deleted is False

    stub = StubReleaseProvider()
    controller = _make_controller(reg, release_provider=stub)
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    # 释放被调用且仅一次
    assert stub.released == ["svc-1"]
    # 协调完成（物理删除成功）
    assert result.done is True
    assert result.requeue_after is None
    # 资源已物理删除
    with pytest.raises(NotFoundError):
        asyncio.run(reg.get("InferenceService", None, "svc-1", "tenant-1"))
    # Allocation 已释放（Active → Released）
    allocs = asyncio.run(reg.list_allocations("tenant-1"))
    assert len(allocs) == 1
    assert allocs[0]["status"] == "Released"
    assert allocs[0]["released_at"] is not None
    assert asyncio.run(reg.allocated_devices()) == frozenset()


@pytest.mark.unit
def test_terminating_releases_only_the_allocation_of_the_deleted_uid():
    """释放按 uid、限本租户：同名资源在其他命名空间 / 租户中的分配不受影响。"""
    reg = SQLiteRegistry()
    services = [
        make_inference_service(namespace="team-a", uid="uid-a"),
        make_inference_service(namespace="team-b", uid="uid-b"),
        make_inference_service(namespace="team-a", uid="uid-c", tenant_id="tenant-2"),
    ]
    for index, service in enumerate(services):
        asyncio.run(reg.put(service))
        _allocate(reg, service, device_id=f"gpu-{index}")
    asyncio.run(reg.request_delete("InferenceService", "team-a", "svc-1", "tenant-1"))
    ctx = _terminating_context(reg, namespace="team-a")

    result = asyncio.run(_make_controller(reg).reconcile(ctx))

    assert result.done is True
    assert asyncio.run(reg.get_active_allocation("uid-a", "tenant-1")) is None
    assert asyncio.run(reg.get_active_allocation("uid-b", "tenant-1")) is not None
    assert asyncio.run(reg.get_active_allocation("uid-c", "tenant-2")) is not None


@pytest.mark.unit
def test_terminating_resource_replaced_by_a_new_uid_is_left_alone():
    """协调前原资源已被删除、同名资源以新 uid 重建：只释放原资源的分配，新资源不动。"""
    reg = SQLiteRegistry()
    original = make_inference_service()
    asyncio.run(reg.put(original))
    _allocate(reg, original)
    asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))
    ctx = _terminating_context(reg)
    asyncio.run(reg.delete("InferenceService", None, "svc-1", "tenant-1"))
    asyncio.run(reg.put(make_inference_service(phase=InferencePhase.PENDING, uid="uid-2")))

    result = asyncio.run(_make_controller(reg).reconcile(ctx))

    assert result.done is True
    survivor = asyncio.run(reg.get("InferenceService", None, "svc-1", "tenant-1"))
    assert survivor.metadata.uid == "uid-2"
    assert asyncio.run(reg.get_active_allocation("uid-1", "tenant-1")) is None
    assert asyncio.run(reg.allocated_devices()) == frozenset()


@pytest.mark.unit
def test_terminating_release_failure_keeps_terminating():
    """release 抛异常 → 保持 Terminating + requeue 重试（不物理删除）。"""
    reg = SQLiteRegistry()
    asyncio.run(reg.put(make_inference_service()))
    asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))

    stub = StubReleaseProvider(fail=True)
    controller = _make_controller(reg, release_provider=stub)
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    assert result.done is False
    assert result.requeue_after == 10.0
    conditions = result.status_patch["conditions"]
    assert any(c["reason"] == "ReleaseFailed" for c in conditions)
    # 资源仍在且保持 Terminating（不物理删除、GPU 不误删）
    row = reg._conn.execute("SELECT phase FROM resources WHERE name='svc-1'").fetchone()
    assert row is not None and row["phase"] == "Terminating"
    # 失败不登记 dedup_key：同 key 下一轮仍会重试
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_terminating_noop_provider_still_deletes():
    """未注入 release_provider（Noop 语义）→ 仅移除 finalizer 后物理删除。"""
    reg = SQLiteRegistry()
    asyncio.run(reg.put(make_inference_service()))
    asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))

    controller = _make_controller(reg)  # release_provider=None
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    assert result.done is True
    row = reg._conn.execute("SELECT phase FROM resources WHERE name='svc-1'").fetchone()
    assert row is None  # 已物理删除


@pytest.mark.unit
def test_terminating_waits_for_other_finalizers():
    """仍有其他 finalizer 持有方 → 移除默认 release finalizer 后等待（requeue）。"""
    reg = SQLiteRegistry()
    svc = make_inference_service()
    svc.finalizers = ["example.com/cleanup", "forgemesh.io/resource-release"]
    asyncio.run(reg.put(svc))
    asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))

    controller = _make_controller(reg)
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    assert result.done is False
    assert result.requeue_after == 5.0
    conditions = result.status_patch["conditions"]
    assert any(c["reason"] == "WaitingFinalizers" for c in conditions)
    # 默认 release finalizer 已被移除，自定义 finalizer 保留
    body = reg._conn.execute("SELECT finalizers FROM resources WHERE name='svc-1'").fetchone()
    assert json.loads(body["finalizers"]) == ["example.com/cleanup"]


@pytest.mark.unit
def test_non_terminating_phase_skips_delete_path():
    """Pending 资源不走删除子流程（走部署推进）。"""
    reg = SQLiteRegistry()
    service = make_inference_service(phase=InferencePhase.PENDING)
    asyncio.run(reg.put(service))
    controller = _controller(
        registry=reg, scheduler=MockScheduler(), workflow=StubWorkflowRuntime()
    )

    result = asyncio.run(controller.reconcile(make_service_context(service)))

    assert result.status_patch["phase"] == "Scheduling"
    row = reg._conn.execute("SELECT phase FROM resources WHERE name='svc-1'").fetchone()
    assert row is not None and row["phase"] == "Pending"
