"""部署闭环测试：控制器管理器、协调器、首次适配调度器、本地工作流运行时与注册表一起运行，
启动器使用测试替身。

* 创建的资源每轮推进一个 phase（``Pending`` → ``Scheduling`` → ``Preparing`` →
  ``Deploying`` → ``Verifying`` → ``Ready``），``status.endpoints`` 是启动器返回的实例；
* 重启（``Ready`` 之后、启动部署的一轮被重放、启动实例的中途）不会启动第二个实例，也
  不会再分配；
* 实例丢失（没有活过重启，或自行退出）：``Ready`` 的服务转为 ``Degraded``（endpoint 标记
  为未就绪），在同一分配上重新部署后回到 ``Ready``，运行成功却还没写入 ``Ready`` 的服务
  同样重新部署；重新部署按退避，失败时说明原因；实例回来了的服务直接回到 ``Ready``；核对的
  结论与启动的重新部署计入指标，退避期间推迟的协调不计入；
* 部署工作流没能启动时保留分配并重试；部署失败时实例被停止、分配释放，资源停在
  ``Failed``，修改 spec 之后从 ``Pending`` 重新部署；补偿没能停止的实例在之后的一轮停止
  （然后释放分配），或在删除资源、重新部署时停止；
* 部署过程中 spec 变化：上一代 spec 的运行被取消，服务以新的 spec 进入 ``Ready``；加速器
  申请变化、持有的设备不再满足时释放分配、重新分配；
* ``Ready`` 的服务 spec 变化：经 ``Updating`` 停止旧实例、部署新的 spec 后回到 ``Ready``；
  更新失败时进入 ``Failed``；策略拒绝更新时服务保持原样；
* 部署中途或 ``Ready`` 之后删除资源：取消运行、停止实例、释放分配、物理删除；绕过受控
  删除的资源，回收时停止它的实例、让分配过期（按结果计数）；
* 在后台运行的控制器自行完成部署与删除，定期核对实例时发现实例退出，自行重新部署。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。“重启”在同一个事件循环中
关闭全部组件，再以同一数据库文件重新组装；启动器对象沿用，相当于实例在控制平面重启后
仍在运行，换一个新的启动器对象则相当于实例随进程结束。
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Collection
from typing import Any

import pytest
from forgemesh.controlplane.controller import ControllerManager
from forgemesh.controlplane.deployment import (
    DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS,
    DeploymentReleaser,
    DeploymentSteps,
    InMemoryLauncher,
    InstanceCheckResult,
    LaunchedInstance,
    LaunchRequest,
    RuntimeLauncher,
    check_instances,
    deployment_run_key,
    find_deployment_runs,
    launched_instance,
    reclaim_orphaned_allocations,
)
from forgemesh.controlplane.metrics import MetricsRegistry
from forgemesh.controlplane.reconciler import (
    REDEPLOY_BACKOFF_RESET_SECONDS,
    ProcessedKeyStore,
    ReconciliationController,
    compute_dedup_key,
)
from forgemesh.controlplane.registry import ResourceKey, SQLiteRegistry
from forgemesh.controlplane.scheduler import FirstFitScheduler, Inventory
from forgemesh.controlplane.sqlite import workflow_db_path
from forgemesh.controlplane.telemetry import ControllerMetrics, InstanceCheck
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta
from forgemesh.core.scheduler import AllocatedDevice
from forgemesh.policy.api import PolicyAction, PolicyDecision, PolicyEngine
from forgemesh.policy.engine import DefaultPolicyEngine
from forgemesh.policy.rules import AuthzRule
from forgemesh.resource.inference import (
    AcceleratorSpec,
    Endpoint,
    InferenceService,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)
from forgemesh.workflow.runtime import RetryPolicy
from forgemesh.workflow.states import WorkflowStatus
from forgemesh.workflowruntime.approval import ApprovalGate
from forgemesh.workflowruntime.compensate import CompensationExecutor
from forgemesh.workflowruntime.exec import StepExecutor, WorkflowExecutor
from forgemesh.workflowruntime.runtime import LocalWorkflowRuntime
from forgemesh.workflowruntime.store import WorkflowStore

from .exposition import parse

KIND = "InferenceService"
TENANT = "tenant-a"
NAMESPACE = "team-a"
NAME = "svc"
UID = "uid-1"
KEY = ResourceKey(TENANT, KIND, NAMESPACE, NAME)
RUN_KEY = deployment_run_key(UID, 1)
DEDUP_KEY = compute_dedup_key(KIND, NAMESPACE, NAME, UID, 1, "reconcile").key
GPU = AllocatedDevice(node_id="node-a", device_id="gpu-0")
LARGE_GPU = AllocatedDevice(node_id="node-a", device_id="gpu-1")

#: 控制器轮询变更、查询部署运行的间隔（秒）。
POLL_SECONDS = 0.01
#: 逐轮驱动时最多协调的轮数。
MAX_PASSES = 10
#: 等待后台任务到达某一点的上限（秒）：出错时测试失败而不是挂起。
WAIT_SECONDS = 5.0
#: 核对实例、重新部署与回收孤立分配的计数器（见 :mod:`forgemesh.controlplane.telemetry`）。
RUNTIME_COUNTERS = (
    "forgemesh_instance_checks_total",
    "forgemesh_instance_check_results_total",
    "forgemesh_lost_instance_redeploys_total",
    "forgemesh_orphaned_allocation_reclaims_total",
)


def _inventory() -> Inventory:
    """单节点、单卡（80 GiB）的清单：第二个分配无处可放。"""
    node = {
        "id": GPU.node_id,
        "devices": [{"id": GPU.device_id, "type": "nvidia-gpu", "memoryGiB": 80}],
    }
    return Inventory.model_validate_json(json.dumps({"nodes": [node]}))


def _two_gpu_inventory() -> Inventory:
    """单节点的清单：一张 24 GiB 的卡（:data:`GPU`）与一张 80 GiB 的卡（:data:`LARGE_GPU`）。"""
    devices = [
        {"id": GPU.device_id, "type": "nvidia-gpu", "memoryGiB": 24},
        {"id": LARGE_GPU.device_id, "type": "nvidia-gpu", "memoryGiB": 80},
    ]
    return Inventory.model_validate_json(
        json.dumps({"nodes": [{"id": GPU.node_id, "devices": devices}]})
    )


def _spec(model: str = "models/llama", min_memory_gib: int = 16) -> InferenceServiceSpec:
    return InferenceServiceSpec(
        model=ModelRef(reference=model),
        runtime=RuntimeRef(provider="vllm"),
        resources=ResourcesSpec(
            accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=min_memory_gib)
        ),
    )


def _service() -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(
            name=NAME, namespace=NAMESPACE, uid=UID, tenant_id=TENANT, generation=1
        ),
        spec=_spec(),
    )


class _ScriptedLauncher(InMemoryLauncher):
    """可让调用失败或在中途阻塞的启动器。

    ``block`` / ``fail`` 中的名称：``launch``（创建实例之前）、``launched``（创建实例
    之后、返回之前，模拟结果落盘前进程退出）、``verify``；``stop_failures`` 为接下来要
    失败的 ``stop`` 调用次数；``down`` 中的实例（实例 id）不在运行，像进程已经退出，
    移出 ``down`` 即实例回来了。
    """

    def __init__(self) -> None:
        super().__init__()
        self.block: set[str] = set()
        self.fail: set[str] = set()
        self.stop_failures = 0
        self.down: set[str] = set()
        self.entered: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.cancelled: list[str] = []

    async def wait_entered(self, name: str) -> None:
        """等到 ``name`` 对应的调用开始。"""
        async with asyncio.timeout(WAIT_SECONDS):
            await self.entered[name].wait()

    async def _gate(self, name: str) -> None:
        self.entered[name].set()
        if name in self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append(name)
                raise
        if name in self.fail:
            raise RuntimeError(f"{name} broke")

    async def launch(self, request: LaunchRequest) -> LaunchedInstance:
        await self._gate("launch")
        instance = await super().launch(request)
        await self._gate("launched")
        return instance

    async def verify(self, instance: LaunchedInstance) -> None:
        await self._gate("verify")
        await super().verify(instance)

    async def stop(self, instance_id: str) -> None:
        if self.stop_failures:
            self.stop_failures -= 1
            raise RuntimeError("stop broke")
        await super().stop(instance_id)

    async def is_running(self, instance_id: str) -> bool:
        return instance_id not in self.down and await super().is_running(instance_id)


class _Clock:
    """手动推进的单调时钟（秒）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _runtime(launcher: RuntimeLauncher, store: WorkflowStore) -> LocalWorkflowRuntime:
    """按部署工作流模块说明组装运行时；失败的步骤立即重试一次。"""
    steps = DeploymentSteps(launcher)
    compensation = CompensationExecutor()
    for step_id, handler in steps.compensations().items():
        compensation.registry.register(step_id, handler)
    retry = RetryPolicy(max_attempts=2, initial_backoff_seconds=0.0, max_backoff_seconds=0.0)
    return LocalWorkflowRuntime(
        store=store,
        executor=WorkflowExecutor(StepExecutor(retry)),
        compensation=compensation,
        approval=ApprovalGate(),
        handler=steps.handle,
    )


class _ControlPlane:
    """一个控制平面进程的组件，组装方式与应用相同（在 ``async with`` 内使用）。

    进入时调用 ``recover()`` 与 ``check_instances(after_restart=True)``（返回值存于
    :attr:`recovered` 与 :attr:`lost`）；退出时按应用的顺序关闭：控制器 → 工作流运行时 →
    启动器 → 工作流存储 → 去重键存储 → 注册表。``clock`` 为协调器重新部署退避所用的时钟；
    控制器管理器运行时每隔 ``instance_check_interval`` 秒核对一次实例。核对实例、重新部署与
    回收与应用一样计入 :attr:`metrics`（见 :meth:`counted`）。
    """

    def __init__(
        self,
        db: str,
        launcher: RuntimeLauncher,
        inventory: Inventory | None = None,
        policy: PolicyEngine | None = None,
        clock: Callable[[], float] = time.monotonic,
        instance_check_interval: float = DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self.launcher = launcher
        self.registry = SQLiteRegistry(db)
        self.processed = ProcessedKeyStore(db)
        self.workflow_store = WorkflowStore(workflow_db_path(db))
        self.runtime = _runtime(launcher, self.workflow_store)
        if policy is None:
            policy = DefaultPolicyEngine(
                authz_rules=[AuthzRule(action="reconcile", effect="allow")]
            )
        release = DeploymentReleaser(self.runtime, launcher)
        self.metrics = MetricsRegistry()
        self._controller_metrics = metrics = ControllerMetrics(self.metrics)
        reconciler = ReconciliationController(
            self.registry,
            policy,
            scheduler=FirstFitScheduler(self.registry, inventory or _inventory()),
            workflow_runtime=self.runtime,
            release_provider=release,
            processed_store=self.processed,
            record_dedup_keys=False,
            workflow_poll_interval=POLL_SECONDS,
            launcher=launcher,
            clock=clock,
            on_redeploy=metrics.lost_instance_redeployed,
        )
        self.manager = ControllerManager(
            self.registry,
            reconciler,
            poll_interval=POLL_SECONDS,
            instance_check=functools.partial(
                check_instances,
                self.registry,
                launcher,
                on_result=functools.partial(metrics.instance_check_result, InstanceCheck.PERIODIC),
            ),
            instance_check_interval=instance_check_interval,
            reclaim=functools.partial(
                reclaim_orphaned_allocations,
                self.registry,
                release,
                on_result=metrics.reclaim_attempted,
            ),
            metrics=metrics,
        )
        self.recovered: list[str] = []
        self.lost: list[ResourceKey] = []

    async def __aenter__(self) -> _ControlPlane:
        self.recovered = await self.runtime.recover()
        self.lost = await check_instances(
            self.registry,
            self.launcher,
            after_restart=True,
            on_result=functools.partial(
                self._controller_metrics.instance_check_result, InstanceCheck.STARTUP
            ),
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.manager.stop()
        await self.runtime.aclose()
        await self.launcher.aclose()
        await self.workflow_store.close()
        await self.processed.close()
        await self.registry.close()

    async def service(self) -> InferenceService:
        return await self.registry.get(KIND, NAMESPACE, NAME, TENANT)

    def counted(self) -> dict[tuple[str, ...], float]:
        """:data:`RUNTIME_COUNTERS` 中不为 0 的序列：``(指标名, 标签值...)`` → 取值，指标名去掉
        ``forgemesh_`` 前缀与 ``_total`` 后缀。"""
        families = parse(self.metrics.render())
        counted: dict[tuple[str, ...], float] = {}
        for name in RUNTIME_COUNTERS:
            short = name.removeprefix("forgemesh_").removesuffix("_total")
            for sample in families[name].samples:
                if sample.value:
                    counted[(short, *sample.labels.values())] = sample.value
        return counted

    async def phase(self) -> str | None:
        """资源的 phase；资源已删除时为 ``None``。"""
        try:
            service = await self.service()
        except NotFoundError:
            return None
        return service.status.phase.value

    async def deploy(self, until: Collection[str] = ("Ready", "Failed")) -> list[str]:
        """逐轮协调直到 phase 属于 ``until``，返回每轮之后变化了的 phase。

        每轮之前等资源全部部署运行的后台执行结束，各轮看到的运行状态因此是确定的；运行被
        阻塞时只能推进到启动它的那一轮（``until`` 含 ``Deploying``）。
        """
        phases: list[str] = []
        for _ in range(MAX_PASSES):
            for deployment in await find_deployment_runs(self.runtime, UID):
                async with asyncio.timeout(WAIT_SECONDS):
                    await self.runtime.wait(deployment.run.run_id)
            await self.manager.process(KEY)
            phase = (await self.service()).status.phase.value
            if not phases or phases[-1] != phase:
                phases.append(phase)
            if phase in until:
                return phases
        raise AssertionError(f"the service did not reach {until} in {MAX_PASSES} passes: {phases}")

    async def delete(self) -> bool:
        """请求删除资源，返回它是否被直接物理删除（否则进入 ``Terminating``）。"""
        return (await self.registry.request_delete(KIND, NAMESPACE, NAME, TENANT)).deleted

    async def update(self, spec: InferenceServiceSpec) -> int:
        """像 ``PUT`` 一样替换资源的 spec，返回新的 generation。"""
        service = await self.registry.update_spec(
            KIND, NAMESPACE, NAME, TENANT, spec.model_dump(mode="json"), labels={}, annotations={}
        )
        return service.metadata.generation


def _ready(service: InferenceService) -> tuple[str, str | None, str | None]:
    """``Ready`` 条件的 ``(status, reason, message)``。"""
    [condition] = [c for c in service.status.conditions if c.type == "Ready"]
    return condition.status, condition.reason, condition.message


async def _assert_gone(plane: _ControlPlane) -> None:
    """资源已物理删除，没有留下 Active 分配，设备全部空闲。"""
    assert await plane.phase() is None
    assert await plane.registry.get_active_allocation(UID, TENANT) is None
    assert await plane.registry.allocated_devices() == frozenset()


async def _wait_for_phase(plane: _ControlPlane, phase: str | None) -> None:
    """等后台运行的控制器把资源推进到 ``phase``（``None`` 为资源已删除）。"""
    async with asyncio.timeout(WAIT_SECONDS):
        while await plane.phase() != phase:
            await asyncio.sleep(POLL_SECONDS)


async def _killed(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("process killed")


# ---------------------------------------------------------------------- #
# 部署
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_created_service_moves_one_phase_per_pass_until_it_serves_its_instance():
    """每轮一个合法的 phase 转移；Ready 时 ``status.endpoints`` 是启动器返回的实例，实例
    拿到了分配的设备。只有最后一轮登记去重键，之后的协调不再分配、不再启动。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            phases = await plane.deploy()

            service = await plane.service()
            [instance] = launcher.running.values()
            [allocation] = await plane.registry.list_allocations(TENANT)
            [request] = launcher.launches
            assert phases == ["Scheduling", "Preparing", "Deploying", "Verifying", "Ready"]
            assert service.status.endpoints == [instance.endpoint()]
            assert _ready(service) == (
                "True",
                "Reconciled",
                f"instance {instance.instance_id} is serving at {instance.url}",
            )
            assert (request.allocation_id, request.devices) == (allocation["id"], (GPU,))
            assert launcher.prepared == [request]
            assert await plane.processed.seen(DEDUP_KEY)

            await plane.manager.process(KEY)
            assert len(launcher.launches) == 1
            assert await plane.registry.list_allocations(TENANT) == [allocation]

    asyncio.run(scenario())


@pytest.mark.unit
def test_a_workflow_that_fails_to_start_is_retried_with_the_same_allocation(monkeypatch):
    """启动部署运行失败（工作流存储被锁）：停在 Preparing、``Ready=False``、保留分配；之后
    的一轮启动成功，资源照常进入 Ready，自始至终只有一个分配。"""
    launcher = _ScriptedLauncher()

    async def locked(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy(until={"Preparing"})
            [allocation] = await plane.registry.list_allocations(TENANT)
            with monkeypatch.context() as patch:
                patch.setattr(plane.workflow_store, "create_run", locked)
                await plane.manager.process(KEY)

            service = await plane.service()
            active = await plane.registry.get_active_allocation(UID, TENANT)
            assert service.status.phase == "Preparing"
            assert _ready(service) == ("False", "WorkflowStartFailed", "database is locked")
            assert active["id"] == allocation["id"]
            assert launcher.launches == []

            assert await plane.deploy() == ["Deploying", "Verifying", "Ready"]
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            assert len(launcher.launches) == 1

    asyncio.run(scenario())


@pytest.mark.unit
def test_a_failed_deployment_stops_its_instance_releases_its_allocation_and_stays_failed():
    """验证用尽重试：工作流补偿停止实例，资源进入 Failed（``Ready=False``，消息是失败的
    步骤），分配释放；没有自动重试，spec 不变时之后的协调不再部署，删除时直接物理删除。"""
    launcher = _ScriptedLauncher()
    launcher.fail.add("verify")

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            assert await plane.deploy() == ["Scheduling", "Preparing", "Deploying", "Failed"]

            service = await plane.service()
            assert _ready(service) == (
                "False",
                "WorkflowFailed",
                "step 'verify-instance' failed: verify broke",
            )
            assert service.status.endpoints == []
            assert launcher.running == {}
            assert len(launcher.stops) == 1
            assert await plane.registry.get_active_allocation(UID, TENANT) is None
            assert await plane.registry.allocated_devices() == frozenset()

            await plane.manager.process(KEY)
            assert await plane.phase() == "Failed"
            assert len(launcher.launches) == 1
            assert len(await plane.registry.list_allocations(TENANT)) == 1

            assert await plane.delete() is True
            await _assert_gone(plane)
            assert len(launcher.stops) == 1

    asyncio.run(scenario())


@pytest.mark.unit
def test_an_instance_that_compensation_could_not_stop_is_stopped_when_the_service_is_deleted():
    """补偿没能停止实例：资源进入 Failed 并保留分配（消息说明实例可能仍在运行），删除时
    进入 Terminating，``DeploymentReleaser`` 停止实例后协调器释放分配并物理删除。"""
    launcher = _ScriptedLauncher()
    launcher.fail.add("verify")
    launcher.stop_failures = 1

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            assert await plane.deploy() == ["Scheduling", "Preparing", "Deploying", "Failed"]
            [instance] = launcher.running.values()
            status, reason, message = _ready(await plane.service())
            assert (status, reason) == ("False", "WorkflowFailed")
            assert f"instance {instance.instance_id} may still be running" in message
            assert await plane.registry.get_active_allocation(UID, TENANT) is not None

            assert await plane.delete() is False
            await plane.manager.process(KEY)

            assert launcher.stops == [instance.instance_id]
            assert launcher.running == {}
            await _assert_gone(plane)

    asyncio.run(scenario())


@pytest.mark.unit
def test_an_instance_that_compensation_could_not_stop_is_stopped_before_the_allocation_is_freed():
    """补偿没能停止实例、资源进入 Failed 之后，下一轮停止实例，然后释放分配；停止失败时保留
    分配，之后的一轮重试。资源停在 Failed，之后的协调不再停止，删除时直接物理删除。"""
    launcher = _ScriptedLauncher()
    launcher.fail.add("verify")
    launcher.stop_failures = 2

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            assert await plane.deploy() == ["Scheduling", "Preparing", "Deploying", "Failed"]
            [instance] = launcher.running.values()

            await plane.manager.process(KEY)
            assert launcher.running == {instance.instance_id: instance}
            assert await plane.registry.get_active_allocation(UID, TENANT) is not None

            await plane.manager.process(KEY)
            assert launcher.running == {}
            assert await plane.registry.get_active_allocation(UID, TENANT) is None
            assert await plane.registry.allocated_devices() == frozenset()

            await plane.manager.process(KEY)
            assert launcher.stops == [instance.instance_id]
            assert await plane.phase() == "Failed"
            assert await plane.delete() is True
            await _assert_gone(plane)

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# 部署过程中 spec 变化
# ---------------------------------------------------------------------- #


async def _block_deployment(plane: _ControlPlane, blocked: str) -> str:
    """部署到 ``blocked`` 对应的调用开始、这一轮之后的 phase，返回该 phase。"""
    plane.launcher.block.add(blocked)
    await plane.deploy(until={"Deploying"})
    await plane.launcher.wait_entered(blocked)
    await plane.manager.process(KEY)
    return await plane.phase()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("blocked", "phase"),
    [("launch", "Deploying"), ("verify", "Verifying")],
    ids=["while-launching", "while-verifying"],
)
def test_a_spec_change_during_the_deployment_deploys_the_new_spec(blocked, phase):
    """部署进行中 spec 变化：phase 不变，上一代 spec 的运行被取消（补偿停止已启动的实例），
    新的 generation 以同一分配启动自己的运行与实例；服务以新的 spec 进入 Ready
    （``observedGeneration`` 即新的 generation）。删除时没有留下任何实例。"""
    launcher = _ScriptedLauncher()
    new_spec = _spec(model="models/mistral")

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            assert await _block_deployment(plane, blocked) == phase
            [allocation] = await plane.registry.list_allocations(TENANT)

            assert await plane.update(new_spec) == 2
            launcher.block.clear()
            await plane.manager.process(KEY)

            service = await plane.service()
            [run] = await find_deployment_runs(plane.runtime, UID, 1)
            assert service.status.phase.value == phase
            assert _ready(service)[:2] == ("False", phase)
            assert _ready(service)[2].startswith("generation 2 replaces the deployment in progress")
            assert run.run.status is WorkflowStatus.CANCELLED
            assert launcher.cancelled == [blocked]

            assert (await plane.deploy())[-1] == "Ready"
            service = await plane.service()
            [instance] = launcher.running.values()
            assert (launcher.launches[-1].instance_key, launcher.launches[-1].spec) == (
                deployment_run_key(UID, 2),
                new_spec,
            )
            assert service.status.endpoints == [instance.endpoint()]
            assert (service.metadata.generation, service.metadata.observed_generation) == (2, 2)
            assert await plane.registry.list_allocations(TENANT) == [allocation]

            assert await plane.delete() is False
            await plane.manager.process(KEY)
            assert launcher.running == {}
            await _assert_gone(plane)

    asyncio.run(scenario())


@pytest.mark.unit
@pytest.mark.parametrize("blocked", [None, "verify"], ids=["while-preparing", "while-verifying"])
def test_an_accelerator_change_during_the_deployment_moves_the_service_to_a_fitting_device(
    blocked,
):
    """部署过程中加速器申请变大（至少 40 GiB），持有的 24 GiB 卡不再满足：之前的部署停止、
    分配释放，新的分配取得 80 GiB 卡，服务以新的 spec 在那张卡上进入 Ready。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher, _two_gpu_inventory()) as plane:
            await plane.registry.put(_service())
            if blocked is None:
                await plane.deploy(until={"Preparing"})
            else:
                assert await _block_deployment(plane, blocked) == "Verifying"
            [previous] = await plane.registry.list_allocations(TENANT)
            assert previous["devices"] == [GPU.model_dump()]

            await plane.update(_spec(min_memory_gib=40))
            launcher.block.clear()
            await plane.manager.process(KEY)
            assert (await plane.deploy())[-1] == "Ready"

            allocations = {
                record["idempotency_key"]: (record["status"], record["devices"])
                for record in await plane.registry.list_allocations(TENANT)
            }
            [instance] = launcher.running.values()
            assert allocations == {
                f"{UID}:1": ("Released", [GPU.model_dump()]),
                f"{UID}:2": ("Active", [LARGE_GPU.model_dump()]),
            }
            assert launcher.launches[-1].devices == (LARGE_GPU,)
            assert (await plane.service()).status.endpoints == [instance.endpoint()]

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# 更新已部署的服务
# ---------------------------------------------------------------------- #


class _SwitchablePolicy(PolicyEngine):
    """按 :attr:`decision` 判定的策略引擎（测试中途切换）。"""

    def __init__(self) -> None:
        self.decision = "allow"

    async def check(self, ctx: Any, action: PolicyAction) -> PolicyDecision:
        return PolicyDecision(decision=self.decision, reason=f"{action.action}: {self.decision}")


@pytest.mark.unit
def test_updating_a_ready_service_redeploys_it_with_the_new_spec_on_the_same_allocation():
    """Ready 的服务 spec 变化：第一轮进入 Updating、endpoint 标记为未就绪，没有副作用（旧
    实例仍在运行）；下一轮停止旧实例，以同一分配启动新的 generation，服务以新的 spec 进入
    Ready（``observedGeneration`` 即新的 generation），登记新 generation 的去重键。"""
    launcher = _ScriptedLauncher()
    new_spec = _spec(model="models/mistral")

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [old] = launcher.running.values()
            [allocation] = await plane.registry.list_allocations(TENANT)

            assert await plane.update(new_spec) == 2
            await plane.manager.process(KEY)
            service = await plane.service()
            assert service.status.phase.value == "Updating"
            assert service.status.endpoints == [old.endpoint().model_copy(update={"ready": False})]
            assert _ready(service) == ("False", "Updating", "deploying generation 2")
            assert (launcher.stops, list(launcher.running)) == ([], [old.instance_id])

            assert await plane.deploy() == ["Updating", "Ready"]
            service = await plane.service()
            [new] = launcher.running.values()
            request = launcher.launches[-1]
            assert launcher.stops == [old.instance_id]
            assert (request.instance_key, request.spec, request.allocation_id) == (
                deployment_run_key(UID, 2),
                new_spec,
                allocation["id"],
            )
            assert service.status.endpoints == [new.endpoint()]
            assert (service.metadata.generation, service.metadata.observed_generation) == (2, 2)
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            assert await plane.processed.seen(
                compute_dedup_key(KIND, NAMESPACE, NAME, UID, 2, "reconcile").key
            )

            await plane.manager.process(KEY)
            assert len(launcher.launches) == 2

    asyncio.run(scenario())


@pytest.mark.unit
def test_an_accelerator_change_moves_a_ready_service_to_a_fitting_device():
    """Ready 的服务加速器申请变大，持有的 24 GiB 卡不再满足：旧实例停止、分配释放，新的
    分配取得 80 GiB 卡，服务以新的 spec 在那张卡上回到 Ready。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher, _two_gpu_inventory()) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [old] = launcher.running.values()

            await plane.update(_spec(min_memory_gib=40))
            assert await plane.deploy() == ["Updating", "Ready"]

            allocations = {
                record["idempotency_key"]: (record["status"], record["devices"])
                for record in await plane.registry.list_allocations(TENANT)
            }
            [new] = launcher.running.values()
            assert allocations == {
                f"{UID}:1": ("Released", [GPU.model_dump()]),
                f"{UID}:2": ("Active", [LARGE_GPU.model_dump()]),
            }
            assert old.instance_id in launcher.stops
            assert launcher.launches[-1].devices == (LARGE_GPU,)
            assert (await plane.service()).status.endpoints == [new.endpoint()]

    asyncio.run(scenario())


@pytest.mark.unit
def test_a_spec_change_during_an_update_deploys_the_latest_spec():
    """更新进行中 spec 再次变化：仍是 Updating，进行中的运行被取消（补偿停止它启动的实例），
    最新的 generation 启动自己的运行，服务以最新的 spec 进入 Ready，只留下一个实例。"""
    launcher = _ScriptedLauncher()
    latest = _spec(model="models/qwen")

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            launcher.block.add("verify")
            launcher.entered.clear()
            await plane.update(_spec(model="models/mistral"))
            await plane.manager.process(KEY)
            await plane.manager.process(KEY)
            await launcher.wait_entered("verify")

            assert await plane.update(latest) == 3
            launcher.block.clear()
            await plane.manager.process(KEY)
            service = await plane.service()
            [updating] = await find_deployment_runs(plane.runtime, UID, 2)
            [run] = await find_deployment_runs(plane.runtime, UID, 3)
            assert service.status.phase.value == "Updating"
            assert _ready(service) == (
                "False",
                "Updating",
                f"deployment workflow run {run.run.run_id} deploys generation 3",
            )
            assert updating.run.status is WorkflowStatus.CANCELLED
            assert launcher.cancelled == ["verify"]

            assert (await plane.deploy())[-1] == "Ready"
            service = await plane.service()
            [instance] = launcher.running.values()
            assert (launcher.launches[-1].instance_key, launcher.launches[-1].spec) == (
                deployment_run_key(UID, 3),
                latest,
            )
            assert service.status.endpoints == [instance.endpoint()]
            assert (service.metadata.generation, service.metadata.observed_generation) == (3, 3)

    asyncio.run(scenario())


@pytest.mark.unit
def test_a_failed_update_fails_the_service_and_releases_its_allocation():
    """更新的运行失败（验证用尽重试）：旧实例已经停止，补偿停止新实例，服务进入 Failed
    （``WorkflowFailed``），``status.endpoints`` 为空，分配释放。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            launcher.fail.add("verify")
            await plane.update(_spec(model="models/mistral"))
            assert await plane.deploy() == ["Updating", "Failed"]

            service = await plane.service()
            assert _ready(service) == (
                "False",
                "WorkflowFailed",
                "step 'verify-instance' failed: verify broke",
            )
            assert service.status.endpoints == []
            assert launcher.running == {}
            assert len(launcher.stops) == 2
            assert await plane.registry.get_active_allocation(UID, TENANT) is None
            assert await plane.registry.allocated_devices() == frozenset()

    asyncio.run(scenario())


@pytest.mark.unit
def test_an_update_the_policy_denies_leaves_the_service_serving_until_it_is_allowed():
    """策略拒绝更新：服务保持 Ready、status 不变（``observedGeneration`` 仍是之前的
    generation，实例继续服务）；策略放行后，下一轮照常开始更新。"""
    launcher = _ScriptedLauncher()
    policy = _SwitchablePolicy()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher, policy=policy) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            before = await plane.service()

            policy.decision = "deny"
            await plane.update(_spec(model="models/mistral"))
            await plane.manager.process(KEY)
            service = await plane.service()
            assert service.status == before.status
            assert service.metadata.observed_generation == 1
            assert len(launcher.launches) == 1

            policy.decision = "allow"
            assert await plane.deploy() == ["Updating", "Ready"]
            assert (await plane.service()).metadata.observed_generation == 2

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# 失败之后修改 spec
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_failed_service_is_deployed_again_once_its_spec_changes():
    """部署失败（分配已释放）之后修改 spec：服务回到 Pending，与新建的资源一样逐轮部署，
    以新的 spec 进入 Ready（``observedGeneration`` 即新的 generation）。"""
    launcher = _ScriptedLauncher()
    launcher.fail.add("verify")
    new_spec = _spec(model="models/mistral")

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            assert (await plane.deploy())[-1] == "Failed"
            launcher.fail.clear()

            assert await plane.update(new_spec) == 2
            assert await plane.deploy() == [
                "Pending",
                "Scheduling",
                "Preparing",
                "Deploying",
                "Verifying",
                "Ready",
            ]
            service = await plane.service()
            [instance] = launcher.running.values()
            assert (launcher.launches[-1].instance_key, launcher.launches[-1].spec) == (
                deployment_run_key(UID, 2),
                new_spec,
            )
            assert service.status.endpoints == [instance.endpoint()]
            assert (service.metadata.generation, service.metadata.observed_generation) == (2, 2)
            assert {
                record["idempotency_key"]: record["status"]
                for record in await plane.registry.list_allocations(TENANT)
            } == {f"{UID}:1": "Released", f"{UID}:2": "Active"}

    asyncio.run(scenario())


@pytest.mark.unit
def test_an_instance_left_behind_by_a_failed_deployment_is_stopped_before_the_redeployment():
    """补偿没能停止实例（服务 Failed、保留分配）之后修改 spec：新的部署复用这份分配，启动
    新的部署运行之前停止遗留的实例，最后只有新的实例在运行。"""
    launcher = _ScriptedLauncher()
    launcher.fail.add("verify")
    launcher.stop_failures = 1

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            assert (await plane.deploy())[-1] == "Failed"
            [left_behind] = launcher.running.values()
            [allocation] = await plane.registry.list_allocations(TENANT)
            launcher.fail.clear()

            await plane.update(_spec(model="models/mistral"))
            assert (await plane.deploy())[-1] == "Ready"
            [instance] = launcher.running.values()
            assert launcher.stops == [left_behind.instance_id]
            assert instance.instance_id != left_behind.instance_id
            assert launcher.launches[-1].allocation_id == allocation["id"]
            assert await plane.registry.list_allocations(TENANT) == [allocation]

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# 重放与重启
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_restarting_a_ready_control_plane_neither_launches_nor_allocates_again(tmp_path):
    """Ready 之后重启（文件库）：没有要恢复的运行，协调不再启动实例、不再分配；工作流存储
    是控制平面数据库旁的 ``*.workflows.db``。"""
    db = str(tmp_path / "cp.db")
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(db, launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            endpoints = (await plane.service()).status.endpoints
            allocations = await plane.registry.list_allocations(TENANT)

        async with _ControlPlane(db, launcher) as plane:
            await plane.manager.process(KEY)
            assert (plane.recovered, plane.lost) == ([], [])
            assert await plane.phase() == "Ready"
            assert (await plane.service()).status.endpoints == endpoints
            assert await plane.registry.list_allocations(TENANT) == allocations

    asyncio.run(scenario())
    assert (tmp_path / "cp.workflows.db").is_file()
    assert len(launcher.launches) == 1
    assert len(launcher.running) == 1


@pytest.mark.unit
def test_replaying_the_pass_that_started_the_deployment_reuses_its_run(tmp_path, monkeypatch):
    """Preparing 一轮启动了部署运行，写入 Deploying 之前进程退出：重启后这一轮被重放，
    分配与运行都按幂等键复用，实例只启动一次。"""
    db = str(tmp_path / "cp.db")
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(db, launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy(until={"Preparing"})
            with monkeypatch.context() as patch:
                patch.setattr(plane.registry, "update_status", _killed)
                with pytest.raises(RuntimeError, match="process killed"):
                    await plane.manager.process(KEY)
            run = await plane.runtime.wait((await plane.runtime.find_run(RUN_KEY)).run_id)
            allocations = await plane.registry.list_allocations(TENANT)
        assert run.status is WorkflowStatus.SUCCEEDED

        async with _ControlPlane(db, launcher) as plane:
            assert await plane.phase() == "Preparing"
            assert await plane.deploy() == ["Deploying", "Verifying", "Ready"]
            assert (await plane.runtime.find_run(RUN_KEY)).run_id == run.run_id
            assert await plane.registry.list_allocations(TENANT) == allocations

    asyncio.run(scenario())
    assert len(launcher.launches) == 1


@pytest.mark.unit
def test_a_restart_in_the_middle_of_a_launch_resumes_the_run_with_the_same_instance(tmp_path):
    """实例已创建、``launch-instance`` 的结果落盘之前进程退出。此前看到运行未结束的一轮
    保持 Deploying、不登记去重键；重启后 ``recover()`` 继续运行，以同一实例幂等键再执行
    这一步，拿回原来的实例。"""
    db = str(tmp_path / "cp.db")
    launcher = _ScriptedLauncher()
    launcher.block.add("launched")

    async def scenario() -> None:
        async with _ControlPlane(db, launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy(until={"Deploying"})
            await launcher.wait_entered("launched")
            await plane.manager.process(KEY)
            assert await plane.phase() == "Deploying"
            assert not await plane.processed.seen(DEDUP_KEY)
            run_id = (await plane.runtime.find_run(RUN_KEY)).run_id

        launcher.block.clear()
        async with _ControlPlane(db, launcher) as plane:
            assert plane.recovered == [run_id]
            assert await plane.deploy() == ["Verifying", "Ready"]
            [instance] = launcher.running.values()
            assert (await plane.service()).status.endpoints == [instance.endpoint()]
            assert len(await plane.registry.list_allocations(TENANT)) == 1

    asyncio.run(scenario())
    assert [request.instance_key for request in launcher.launches] == [RUN_KEY, RUN_KEY]
    assert launcher.stops == []


@pytest.mark.unit
def test_an_instance_lost_between_a_successful_run_and_ready_is_deployed_again(tmp_path):
    """运行已成功、写入 Ready 之前重启，实例没有活过重启（新启动器不认识它）：服务仍是
    Verifying，停止之前的部署，以同一分配再部署一次（幂等键带上次数），不把不存在的实例
    写进 ``status.endpoints``；之后以新实例进入 Ready 并登记去重键。"""
    db = str(tmp_path / "cp.db")
    fresh = _ScriptedLauncher()

    async def scenario() -> LaunchedInstance:
        async with _ControlPlane(db, _ScriptedLauncher()) as plane:
            await plane.registry.put(_service())
            assert await plane.deploy(until={"Verifying"}) == [
                "Scheduling",
                "Preparing",
                "Deploying",
                "Verifying",
            ]
            run = await plane.runtime.find_run(RUN_KEY)
            assert run.status is WorkflowStatus.SUCCEEDED
            lost = launched_instance(run)
            [allocation] = await plane.registry.list_allocations(TENANT)

        async with _ControlPlane(db, fresh) as plane:
            await plane.manager.process(KEY)
            service = await plane.service()
            redeployment = await plane.runtime.find_run(deployment_run_key(UID, 1, 2))
            assert service.status.phase.value == "Verifying"
            assert service.status.endpoints == []
            assert _ready(service) == (
                "False",
                "InstanceLost",
                f"deployment workflow run {redeployment.run_id} redeploys the service (attempt 2)",
            )

            assert await plane.deploy() == ["Ready"]
            [instance] = fresh.running.values()
            assert (await plane.service()).status.endpoints == [instance.endpoint()]
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            assert await plane.processed.seen(DEDUP_KEY)
            assert plane.counted() == {("lost_instance_redeploys",): 1}
        return lost

    lost = asyncio.run(scenario())
    assert [request.instance_key for request in fresh.launches] == [deployment_run_key(UID, 1, 2)]
    assert fresh.stops == [lost.instance_id]


@pytest.mark.unit
def test_a_ready_service_whose_instance_did_not_survive_a_restart_is_deployed_again(tmp_path):
    """Ready 之后重启，实例没有活过重启：启动时服务转为 Degraded，endpoint 标记为未就绪，
    ``Ready=False``（InstanceLost）；控制器停止（已不存在的）实例，在同一分配上再部署一次，
    服务以新实例回到 Ready。"""
    db = str(tmp_path / "cp.db")
    fresh = _ScriptedLauncher()

    async def scenario() -> tuple[Endpoint, Endpoint]:
        async with _ControlPlane(db, _ScriptedLauncher()) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [endpoint] = (await plane.service()).status.endpoints
            [allocation] = await plane.registry.list_allocations(TENANT)
            assert plane.counted() == {}

        async with _ControlPlane(db, fresh) as plane:
            assert plane.lost == [KEY]
            service = await plane.service()
            assert service.status.phase.value == "Degraded"
            assert service.status.endpoints == [endpoint.model_copy(update={"ready": False})]
            assert _ready(service) == (
                "False",
                "InstanceLost",
                f"instance {endpoint.instanceId} at {endpoint.url} did not survive the control "
                "plane restart; redeploying the service",
            )

            assert await plane.deploy() == ["Degraded", "Ready"]
            [redeployed] = (await plane.service()).status.endpoints
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            assert plane.counted() == {
                ("instance_check_results", "startup", "lost"): 1,
                ("lost_instance_redeploys",): 1,
            }
        return endpoint, redeployed

    endpoint, redeployed = asyncio.run(scenario())
    [instance] = fresh.running.values()
    assert redeployed == instance.endpoint()
    assert [request.instance_key for request in fresh.launches] == [deployment_run_key(UID, 1, 2)]
    assert fresh.stops == [endpoint.instanceId]


@pytest.mark.unit
def test_only_the_endpoints_whose_instance_is_gone_are_marked_not_ready():
    """没有 ``instanceId`` 的 endpoint 无从确认，同样算作丢失；仍在运行的实例保持就绪。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> InferenceService:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [running] = (await plane.service()).status.endpoints
            unknown = Endpoint(url="http://10.0.0.9:8000", protocol="openai-chat")
            await plane.registry.update_status(
                KIND,
                NAMESPACE,
                NAME,
                TENANT,
                {"endpoints": [unknown.model_dump(mode="json"), running.model_dump(mode="json")]},
            )

            assert await check_instances(plane.registry, launcher) == [KEY]
            return await plane.service()

    service = asyncio.run(scenario())
    [instance] = launcher.running.values()
    assert service.status.phase.value == "Degraded"
    assert [(e.url, e.ready) for e in service.status.endpoints] == [
        ("http://10.0.0.9:8000", False),
        (instance.url, True),
    ]
    assert _ready(service)[2] == (
        "the instance at http://10.0.0.9:8000 is not running any more; redeploying the service"
    )


@pytest.mark.unit
def test_a_service_changed_while_its_instances_are_checked_is_left_alone(monkeypatch):
    """检查期间资源被修改（CAS 冲突）：跳过它，不覆盖新的状态，结论为 ``conflict``。"""
    results: list[InstanceCheckResult] = []

    async def scenario() -> None:
        async with _ControlPlane(":memory:", _ScriptedLauncher()) as plane:
            await plane.registry.put(_service())
            await plane.deploy()

            async def conflict(*args: Any, **kwargs: Any) -> Any:
                raise ConflictError("changed meanwhile")

            monkeypatch.setattr(plane.registry, "update_status", conflict)
            lost = await check_instances(
                plane.registry, _ScriptedLauncher(), on_result=results.append
            )
            assert lost == []
            monkeypatch.undo()
            assert await plane.phase() == "Ready"

    asyncio.run(scenario())
    assert results == [InstanceCheckResult.CONFLICT]


# ---------------------------------------------------------------------- #
# 实例丢失
# ---------------------------------------------------------------------- #


async def _lose_the_instance(plane: _ControlPlane, launcher: _ScriptedLauncher) -> LaunchedInstance:
    """服务的实例退出，检查实例时服务转为 Degraded；返回退出的实例。"""
    [instance] = launcher.running.values()
    launcher.down.add(instance.instance_id)
    assert await check_instances(plane.registry, launcher) == [KEY]
    return instance


@pytest.mark.unit
def test_a_service_whose_instance_exits_is_degraded_and_deployed_again_on_its_allocation():
    """Ready 的服务实例自行退出：检查时转为 Degraded；下一轮停止之前的部署，以同一分配、同一
    代 spec 再部署一次（幂等键带上次数），清空 ``status.endpoints``，服务以新实例回到 Ready。
    部署进入 Ready 时登记的去重键不挡住恢复，回到 Ready 也不登记恢复的去重键。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [allocation] = await plane.registry.list_allocations(TENANT)
            first = await _lose_the_instance(plane, launcher)
            service = await plane.service()
            assert service.status.phase.value == "Degraded"
            assert service.status.endpoints == [
                first.endpoint().model_copy(update={"ready": False})
            ]
            assert _ready(service) == (
                "False",
                "InstanceLost",
                f"instance {first.instance_id} at {first.url} is not running any more; "
                "redeploying the service",
            )

            await plane.manager.process(KEY)
            service = await plane.service()
            redeployment = await plane.runtime.find_run(deployment_run_key(UID, 1, 2))
            assert service.status.phase.value == "Degraded"
            assert service.status.endpoints == []
            assert _ready(service) == (
                "False",
                "InstanceLost",
                f"deployment workflow run {redeployment.run_id} redeploys the service (attempt 2)",
            )
            assert launcher.stops == [first.instance_id]

            assert await plane.deploy() == ["Ready"]
            service = await plane.service()
            [second] = launcher.running.values()
            assert service.status.endpoints == [second.endpoint()]
            assert _ready(service)[:2] == ("True", "Reconciled")
            assert (service.metadata.generation, service.metadata.observed_generation) == (1, 1)
            assert launcher.launches[-1].allocation_id == allocation["id"]
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            recover = compute_dedup_key(KIND, NAMESPACE, NAME, UID, 1, "recover").key
            assert not await plane.processed.seen(recover)

    asyncio.run(scenario())


@pytest.mark.unit
def test_a_degraded_service_whose_instance_is_back_returns_to_ready_without_a_redeployment():
    """实例回来了（启动器又报告它在运行）：检查实例时交给控制器（结论为 ``back``），服务以
    原来的实例回到 Ready，不停止、不启动实例；实例仍不在运行时检查不再报告这个服务，也没有
    结论。"""
    launcher = _ScriptedLauncher()
    results: list[InstanceCheckResult] = []

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            instance = await _lose_the_instance(plane, launcher)
            check = functools.partial(
                check_instances, plane.registry, launcher, on_result=results.append
            )
            assert await check() == []

            launcher.down.clear()
            assert await check() == [KEY]
            assert await plane.phase() == "Degraded"
            await plane.manager.process(KEY)

            service = await plane.service()
            assert service.status.phase.value == "Ready"
            assert service.status.endpoints == [instance.endpoint()]
            assert (len(launcher.launches), launcher.stops) == (1, [])
            assert plane.counted() == {}

    asyncio.run(scenario())
    assert results == [InstanceCheckResult.BACK]


@pytest.mark.unit
def test_redeployments_back_off_up_to_a_bound_and_start_over_after_a_quiet_period():
    """同一代 spec 反复丢失实例：第一次立即重新部署，之后与上一次开始至少相隔 10 秒、每次
    翻倍、最多 300 秒，等待期间不启动运行（也不计入重新部署）；上一次开始已过 600 秒时再丢失
    实例立即重新部署。"""
    launcher = _ScriptedLauncher()
    clock = _Clock()

    async def scenario() -> tuple[list[int], dict[tuple[str, ...], float]]:
        async with _ControlPlane(":memory:", launcher, clock=clock) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            for wait in (0.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0):
                await _lose_the_instance(plane, launcher)
                if wait:
                    started = len(await find_deployment_runs(plane.runtime, UID))
                    clock.now += wait - 1
                    await plane.manager.process(KEY)
                    assert len(await find_deployment_runs(plane.runtime, UID)) == started
                    clock.now += 1
                assert await plane.deploy() == ["Degraded", "Ready"]

            clock.now += REDEPLOY_BACKOFF_RESET_SECONDS
            await _lose_the_instance(plane, launcher)
            assert await plane.deploy() == ["Degraded", "Ready"]
            runs = await find_deployment_runs(plane.runtime, UID)
            return [deployment.attempt for deployment in runs], plane.counted()

    attempts, counted = asyncio.run(scenario())
    assert attempts == list(range(1, 11))
    assert counted == {("lost_instance_redeploys",): 9}


@pytest.mark.unit
def test_a_failed_redeployment_reports_why_and_is_retried_after_the_backoff():
    """重新部署的运行失败（启动实例用尽重试）：服务仍是 Degraded、继续持有分配，等待退避期间
    ``Ready=False``（WorkflowFailed）说明原因，只写一次；退避结束后再部署一次，成功后回到
    Ready。"""
    launcher = _ScriptedLauncher()
    clock = _Clock()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher, clock=clock) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [allocation] = await plane.registry.list_allocations(TENANT)
            await _lose_the_instance(plane, launcher)
            launcher.fail.add("launch")
            await plane.manager.process(KEY)

            assert await plane.deploy(until={"Degraded"}) == ["Degraded"]
            service = await plane.service()
            assert _ready(service) == (
                "False",
                "WorkflowFailed",
                "step 'launch-instance' failed: launch broke; redeploying the service after a "
                "backoff",
            )
            assert service.status.endpoints == []
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            await plane.manager.process(KEY)
            unchanged = await plane.service()
            assert unchanged.metadata.resourceVersion == service.metadata.resourceVersion

            launcher.fail.clear()
            clock.now += 10
            assert await plane.deploy() == ["Degraded", "Ready"]
            [instance] = launcher.running.values()
            assert (await plane.service()).status.endpoints == [instance.endpoint()]
            assert [d.attempt for d in await find_deployment_runs(plane.runtime, UID)] == [1, 2, 3]
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            assert plane.counted() == {("lost_instance_redeploys",): 2}

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# 删除
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("blocked", "phase", "launched"),
    [("launch", "Deploying", 0), ("verify", "Verifying", 1)],
    ids=["while-launching", "while-verifying"],
)
def test_deleting_a_service_during_its_deployment_leaves_nothing_behind(blocked, phase, launched):
    """部署进行中删除：``DeploymentReleaser`` 取消运行（中断正在执行的步骤，补偿停止已启动
    的实例），协调器释放分配并物理删除，没有留下实例或分配。"""
    launcher = _ScriptedLauncher()
    launcher.block.add(blocked)

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy(until={"Deploying"})
            await launcher.wait_entered(blocked)
            await plane.manager.process(KEY)
            assert await plane.phase() == phase

            assert await plane.delete() is False
            await plane.manager.process(KEY)

            run = await plane.runtime.find_run(RUN_KEY)
            assert run.status is WorkflowStatus.CANCELLED
            assert launcher.cancelled == [blocked]
            assert len(launcher.launches) == len(launcher.stops) == launched
            assert launcher.running == {}
            await _assert_gone(plane)

    asyncio.run(scenario())


@pytest.mark.unit
def test_deleting_a_ready_service_stops_its_instance_and_releases_its_allocation():
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [instance] = launcher.running.values()
            [allocation] = await plane.registry.list_allocations(TENANT)

            assert await plane.delete() is False
            await plane.manager.process(KEY)

            assert launcher.stops == [instance.instance_id]
            assert launcher.running == {}
            assert (await plane.registry.get_allocation(allocation["id"]))["status"] == "Released"
            await _assert_gone(plane)

    asyncio.run(scenario())


@pytest.mark.unit
@pytest.mark.parametrize("stop_failures", [0, 1])
def test_the_allocation_of_a_service_removed_without_its_release_is_reclaimed(stop_failures):
    """资源绕过受控删除被物理删除（``delete()``）：回收孤立的分配先停止它的实例，再让分配
    过期（设备空闲）；停止失败时保留分配，下一次回收重试。资源仍存在时回收不触碰它的分配。
    每个孤立分配的每次回收按结果计数。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.registry.put(_service())
            await plane.deploy()
            [instance] = launcher.running.values()
            [allocation] = await plane.registry.list_allocations(TENANT)
            await plane.manager.reclaim()
            assert launcher.running == {instance.instance_id: instance}
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            assert plane.counted() == {}

            await plane.registry.delete(KIND, NAMESPACE, NAME, TENANT)
            launcher.stop_failures = stop_failures
            for _ in range(stop_failures):
                await plane.manager.reclaim()
                assert await plane.registry.get_active_allocation(UID, TENANT) is not None
            await plane.manager.reclaim()

            assert launcher.stops == [instance.instance_id]
            assert launcher.running == {}
            assert (await plane.registry.get_allocation(allocation["id"]))["status"] == "Expired"
            assert await plane.registry.allocated_devices() == frozenset()
            reclaims = {"reclaimed": 1, "failed": stop_failures}
            assert plane.counted() == {
                ("orphaned_allocation_reclaims", outcome): count
                for outcome, count in reclaims.items()
                if count
            }

    asyncio.run(scenario())


@pytest.mark.unit
def test_a_running_controller_deploys_and_deletes_a_service_on_its_own():
    """控制器在后台运行，各轮由 ``requeue_after`` 与变更轮询安排（测试不逐轮驱动）：创建的
    资源到达 Ready，删除后实例停止、分配释放、资源消失。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> None:
        async with _ControlPlane(":memory:", launcher) as plane:
            await plane.manager.start()
            await plane.registry.put(_service())
            await _wait_for_phase(plane, "Ready")
            [instance] = launcher.running.values()
            assert (await plane.service()).status.endpoints == [instance.endpoint()]

            assert await plane.delete() is False
            await _wait_for_phase(plane, None)
            assert launcher.stops == [instance.instance_id]
            await _assert_gone(plane)

    asyncio.run(scenario())


@pytest.mark.unit
def test_a_running_controller_deploys_a_service_again_once_its_instance_exits():
    """控制器在后台运行、定期核对实例（测试既不逐轮驱动，也不自己核对）：实例退出之后，服务
    以同一分配上的新实例回到 Ready，退出的实例被停止。定期核对按次数计数，丢失的实例与
    重新部署各计一次。"""
    launcher = _ScriptedLauncher()

    async def serving(plane: _ControlPlane) -> list[Endpoint]:
        service = await plane.service()
        return service.status.endpoints if service.status.phase.value == "Ready" else []

    async def scenario() -> None:
        async with _ControlPlane(
            ":memory:", launcher, instance_check_interval=POLL_SECONDS
        ) as plane:
            await plane.manager.start()
            await plane.registry.put(_service())
            await _wait_for_phase(plane, "Ready")
            [allocation] = await plane.registry.list_allocations(TENANT)
            [first] = launcher.running.values()

            launcher.down.add(first.instance_id)
            async with asyncio.timeout(WAIT_SECONDS):
                while (endpoints := await serving(plane)) in ([], [first.endpoint()]):
                    await asyncio.sleep(POLL_SECONDS)

            [second] = launcher.running.values()
            assert endpoints == [second.endpoint()]
            assert launcher.stops == [first.instance_id]
            assert await plane.registry.list_allocations(TENANT) == [allocation]
            counted = plane.counted()
            assert counted.pop(("instance_checks", "completed")) >= 1
            assert counted == {
                ("instance_check_results", "periodic", "lost"): 1,
                ("lost_instance_redeploys",): 1,
            }

    asyncio.run(scenario())
