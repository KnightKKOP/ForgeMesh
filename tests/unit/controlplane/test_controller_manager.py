"""控制器管理器单元测试。

覆盖改进计划 4.10 的验收项：新资源在有限时间内得到带原因的 status；重启后
从注册中心重建工作队列；同一 generation 不重复产生副作用。另覆盖 status 回写
的 CAS 冲突处理、去重键的登记时机、受控删除、重新排队、失败退避、定期核对实例、
回收孤立的分配与优雅停止。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from forgemesh.controlplane.controller import ControllerManager, WorkQueue
from forgemesh.controlplane.deployment import LAUNCH_INSTANCE, LaunchedInstance
from forgemesh.controlplane.outbox import OutboxEvent
from forgemesh.controlplane.reconciler import (
    ProcessedKeyStore,
    ReconciliationController,
    compute_dedup_key,
)
from forgemesh.controlplane.registry import (
    DEFAULT_RELEASE_FINALIZER,
    ResourceKey,
    SQLiteRegistry,
)
from forgemesh.core.errors import NotFoundError
from forgemesh.core.objects import Condition, ObjectMeta, Resource
from forgemesh.core.scheduler import AllocationRef, ScheduleRequest
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
from forgemesh.workflow.runtime import RunInfo, RunRef
from forgemesh.workflow.states import StepStatus, WorkflowStatus

KIND = "InferenceService"
TENANT = "tenant-a"
NAMESPACE = "default"

#: 一次部署的协调轮数：Pending → Scheduling → Preparing → Deploying → Verifying → Ready。
DEPLOYMENT_PASSES = 5

INSTANCE = LaunchedInstance(
    instance_id="instance-1", url="http://10.0.0.5:8000", protocol=EndpointProtocol.OPENAI_CHAT
)


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _service(name: str = "svc", phase: InferencePhase = InferencePhase.PENDING) -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(name=name, namespace=NAMESPACE, tenant_id=TENANT, uid=f"uid-{name}"),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=phase),
    )


def _key(name: str = "svc") -> ResourceKey:
    return ResourceKey(TENANT, KIND, NAMESPACE, name)


class AllowPolicy:
    async def check(self, ctx, action):  # noqa: ANN001
        return PolicyDecision(decision="allow", reason="ok")


class DenyPolicy:
    async def check(self, ctx, action):  # noqa: ANN001
        return PolicyDecision(decision="deny", reason="quota exceeded")


class FailingPolicy:
    def __init__(self) -> None:
        self.calls = 0

    async def check(self, ctx, action):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("policy backend unavailable")


class ConcurrentWritePolicy:
    """协调期间模拟一次并发写入：改标签，可选地同时递增 generation。"""

    def __init__(self, registry: SQLiteRegistry, bump_generation: bool = False) -> None:
        self._registry = registry
        self._bump_generation = bump_generation

    async def check(self, ctx, action):  # noqa: ANN001
        current = await self._registry.get(KIND, NAMESPACE, "svc", TENANT)
        current.metadata.labels["touched"] = "yes"
        if self._bump_generation:
            current.metadata.generation += 1
        await self._registry.put(current, expected_version=current.metadata.resourceVersion)
        return PolicyDecision(decision="allow", reason="ok")


class RecordingPolicy:
    """放行并记录每次协调的关联 ID。"""

    def __init__(self) -> None:
        self.correlation_ids: list[str] = []

    async def check(self, ctx, action):  # noqa: ANN001
        self.correlation_ids.append(ctx.correlation_id)
        return PolicyDecision(decision="allow", reason="ok")


class GatedPolicy:
    """在放行前阻塞，模拟停止时仍在进行的协调。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def check(self, ctx, action):  # noqa: ANN001
        self.entered.set()
        await self.release.wait()
        return PolicyDecision(decision="allow", reason="ok")


class CountingScheduler:
    """同一幂等键返回同一分配（与 FirstFitScheduler 一致），记录调用次数。"""

    def __init__(self) -> None:
        self.calls = 0
        self.allocations: dict[str, AllocationRef] = {}

    @property
    def allocate_count(self) -> int:
        """建立过的分配数（同一幂等键的重放不计）。"""
        return len(self.allocations)

    async def allocate(self, req: ScheduleRequest) -> AllocationRef:
        self.calls += 1
        if req.idempotency_key not in self.allocations:
            self.allocations[req.idempotency_key] = AllocationRef(
                allocation_id=f"alloc-{self.allocate_count + 1}", resource_id=req.resource_ref.uid
            )
        return self.allocations[req.idempotency_key]


class RecordingWorkflow:
    """记录启动的部署；每个运行一启动就视为成功，启动了 :data:`INSTANCE`。"""

    def __init__(self) -> None:
        self.idempotency_keys: list[str] = []

    async def start(self, defn, inputs, idempotency_key):  # noqa: ANN001
        self.idempotency_keys.append(idempotency_key)
        return RunRef(run_id=idempotency_key, definition_version=1)

    async def find_runs(self, key_prefix: str) -> dict[str, RunInfo]:
        return {
            key: RunInfo(
                run_id=key,
                status=WorkflowStatus.SUCCEEDED,
                steps={LAUNCH_INSTANCE: StepStatus.SUCCEEDED},
                outputs={LAUNCH_INSTANCE: INSTANCE.model_dump(mode="json")},
            )
            for key in self.idempotency_keys
            if key.startswith(key_prefix)
        }


class RecordingRelease:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def release(self, resource: Resource) -> None:
        self.released.append(resource.metadata.name)


class RecordingQueue(WorkQueue[ResourceKey]):
    def __init__(self) -> None:
        super().__init__()
        self.added: list[ResourceKey] = []
        self.delays: list[tuple[ResourceKey, float]] = []

    def add(self, key: ResourceKey) -> None:
        self.added.append(key)
        super().add(key)

    def add_after(self, key: ResourceKey, delay: float) -> None:
        self.delays.append((key, delay))
        super().add_after(key, delay)


def _manager(
    registry: SQLiteRegistry,
    *,
    policy: Any = None,
    scheduler: Any = None,
    workflow: Any = None,
    release: Any = None,
    store: ProcessedKeyStore | None = None,
    **kwargs: Any,
) -> ControllerManager:
    if workflow is not None and release is None:
        release = RecordingRelease()
    reconciler = ReconciliationController(
        registry=registry,
        policy_engine=policy or AllowPolicy(),
        scheduler=scheduler,
        workflow_runtime=workflow,
        release_provider=release,
        processed_store=store,
        record_dedup_keys=False,
    )
    return ControllerManager(registry, reconciler, **kwargs)


async def _conditions(registry: SQLiteRegistry, name: str = "svc") -> list[Condition]:
    service = await registry.get(KIND, NAMESPACE, name, TENANT)
    return service.status.conditions


async def _eventually(check: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


@pytest.mark.unit
def test_manager_needs_a_reconciler_that_leaves_dedup_keys_to_it() -> None:
    registry = SQLiteRegistry()
    reconciler = ReconciliationController(registry=registry, policy_engine=AllowPolicy())

    with pytest.raises(ValueError):
        ControllerManager(registry, reconciler)


@pytest.mark.unit
@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_manager_needs_a_positive_instance_check_interval(interval: float) -> None:
    registry = SQLiteRegistry()
    reconciler = ReconciliationController(
        registry=registry, policy_engine=AllowPolicy(), record_dedup_keys=False
    )

    with pytest.raises(ValueError, match="instance_check_interval must be > 0"):
        ControllerManager(registry, reconciler, instance_check_interval=interval)


@pytest.mark.unit
def test_new_service_gets_a_reasoned_status_instead_of_staying_pending() -> None:
    async def scenario() -> Resource:
        registry = SQLiteRegistry()
        manager = _manager(registry, poll_interval=0.01)
        await manager.start()
        try:
            await registry.put(_service())

            async def reported() -> bool:
                return bool(await _conditions(registry))

            await _eventually(reported)
        finally:
            await manager.stop()
        service = await registry.get(KIND, NAMESPACE, "svc", TENANT)
        await registry.close()
        return service

    service = _run(scenario())
    [ready] = service.status.conditions
    assert (ready.type, ready.status, ready.reason) == ("Ready", "False", "SchedulerUnavailable")
    assert service.status.phase == "Pending"
    assert service.status.observedGeneration == service.metadata.generation


@pytest.mark.unit
def test_work_queue_is_rebuilt_from_the_registry_after_a_restart(tmp_path) -> None:
    db_path = str(tmp_path / "cp.db")

    async def scenario() -> None:
        before_crash = SQLiteRegistry(db_path)
        await before_crash.put(_service("a"))
        await before_crash.put(_service("b"))
        await before_crash.close()

        registry = SQLiteRegistry(db_path)
        manager = _manager(registry, poll_interval=60)
        await manager.start()
        try:

            async def both_reported() -> bool:
                return bool(await _conditions(registry, "a")) and bool(
                    await _conditions(registry, "b")
                )

            await _eventually(both_reported)
        finally:
            await manager.stop()
            await registry.close()

    _run(scenario())


@pytest.mark.unit
def test_same_generation_is_not_reconciled_twice_even_across_restarts(tmp_path) -> None:
    db_path = str(tmp_path / "cp.db")
    scheduler = CountingScheduler()
    workflow = RecordingWorkflow()

    async def scenario() -> Resource:
        registry = SQLiteRegistry(db_path)
        store = ProcessedKeyStore(db_path)
        await registry.put(_service())
        manager = _manager(registry, scheduler=scheduler, workflow=workflow, store=store)
        for _ in range(DEPLOYMENT_PASSES + 1):
            await manager.process(_key())
        await store.close()
        await registry.close()

        registry = SQLiteRegistry(db_path)
        store = ProcessedKeyStore(db_path)
        restarted = _manager(registry, scheduler=scheduler, workflow=workflow, store=store)
        await restarted.process(_key())
        service = await registry.get(KIND, NAMESPACE, "svc", TENANT)
        await store.close()
        await registry.close()
        return service

    service = _run(scenario())
    assert (scheduler.allocate_count, scheduler.calls) == (1, 2)
    assert len(workflow.idempotency_keys) == 1
    assert service.status.phase == "Ready"
    assert [condition.reason for condition in service.status.conditions] == ["Reconciled"]


@pytest.mark.unit
def test_dedup_key_is_recorded_only_after_the_status_is_written(monkeypatch) -> None:
    async def failing_write(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("disk I/O error")

    async def scenario() -> None:
        registry = SQLiteRegistry()
        store = ProcessedKeyStore()
        created = await registry.put(_service())
        dedup_key = compute_dedup_key(
            KIND, NAMESPACE, "svc", created.metadata.uid, created.metadata.generation, "reconcile"
        ).key
        manager = _manager(
            registry, scheduler=CountingScheduler(), workflow=RecordingWorkflow(), store=store
        )
        for _ in range(DEPLOYMENT_PASSES - 1):
            await manager.process(_key())
        assert await store.seen(dedup_key) is False

        monkeypatch.setattr(registry, "update_status", failing_write)
        with pytest.raises(RuntimeError):
            await manager.process(_key())
        assert await store.seen(dedup_key) is False

        monkeypatch.undo()
        await manager.process(_key())
        assert await store.seen(dedup_key) is True
        assert [condition.reason for condition in await _conditions(registry)] == ["Reconciled"]
        await store.close()
        await registry.close()

    _run(scenario())


@pytest.mark.unit
def test_status_write_retries_after_a_concurrent_write_to_the_same_generation() -> None:
    async def scenario() -> Resource:
        registry = SQLiteRegistry()
        await registry.put(_service())
        manager = _manager(registry, policy=ConcurrentWritePolicy(registry))
        await manager.process(_key())
        service = await registry.get(KIND, NAMESPACE, "svc", TENANT)
        await registry.close()
        return service

    service = _run(scenario())
    assert [condition.reason for condition in service.status.conditions] == ["SchedulerUnavailable"]
    assert service.metadata.labels == {"touched": "yes"}
    assert service.metadata.resourceVersion == "2"


@pytest.mark.unit
def test_result_for_an_outdated_generation_is_dropped_and_requeued() -> None:
    async def scenario() -> tuple[Resource, int]:
        registry = SQLiteRegistry()
        queue: WorkQueue[ResourceKey] = WorkQueue()
        await registry.put(_service())
        manager = _manager(
            registry, policy=ConcurrentWritePolicy(registry, bump_generation=True), queue=queue
        )
        await manager.process(_key())
        service = await registry.get(KIND, NAMESPACE, "svc", TENANT)
        await registry.close()
        return service, len(queue)

    service, queued = _run(scenario())
    assert service.status.conditions == []
    assert service.metadata.generation == 1
    assert queued == 1


@pytest.mark.unit
def test_deleted_service_is_released_and_removed() -> None:
    async def scenario() -> list[str]:
        registry = SQLiteRegistry()
        await registry.put(_service(phase=InferencePhase.READY))
        await registry.request_delete(KIND, NAMESPACE, "svc", TENANT)
        release = RecordingRelease()
        manager = _manager(registry, release=release)

        await manager.process(_key())

        with pytest.raises(NotFoundError):
            await registry.get(KIND, NAMESPACE, "svc", TENANT)
        await registry.close()
        return release.released

    assert _run(scenario()) == ["svc"]


@pytest.mark.unit
def test_status_write_emits_an_event_with_the_correlation_id_of_the_reconcile() -> None:
    async def scenario() -> tuple[list[str], list[OutboxEvent]]:
        registry = SQLiteRegistry()
        await registry.put(_service(), correlation_id="corr-create")
        policy = RecordingPolicy()
        await _manager(registry, policy=policy).process(_key())
        events = registry.outbox.get_pending()
        await registry.close()
        return policy.correlation_ids, events

    [correlation_id], (created, status) = _run(scenario())
    assert created.payload["correlationId"] == "corr-create"
    assert status.event_type == "inference.service.status_updated"
    assert status.payload["correlationId"] == correlation_id
    [ready] = status.payload["data"]["resource"]["status"]["conditions"]
    assert ready["reason"] == "SchedulerUnavailable"


@pytest.mark.unit
def test_finalizer_removal_and_its_status_share_the_correlation_id_of_the_reconcile() -> None:
    async def scenario() -> list[OutboxEvent]:
        registry = SQLiteRegistry()
        service = _service(phase=InferencePhase.READY)
        service.finalizers = [DEFAULT_RELEASE_FINALIZER, "example.com/cleanup"]
        await registry.put(service)
        await registry.request_delete(KIND, NAMESPACE, "svc", TENANT, correlation_id="corr-delete")
        await _manager(registry, release=RecordingRelease()).process(_key())
        events = registry.outbox.get_pending()
        await registry.close()
        return events

    events = _run(scenario())
    assert [event.event_type for event in events] == [
        "inference.service.created",
        "inference.service.terminating",
        "inference.service.updated",
        "inference.service.status_updated",
    ]
    _, terminating, updated, status = (event.payload for event in events)
    assert terminating["correlationId"] == "corr-delete"
    assert updated["data"]["resource"]["finalizers"] == ["example.com/cleanup"]
    assert updated["correlationId"] == status["correlationId"] != "corr-delete"
    [waiting] = status["data"]["resource"]["status"]["conditions"]
    assert waiting["reason"] == "WaitingFinalizers"


@pytest.mark.unit
def test_only_non_status_changes_of_managed_kinds_are_queued() -> None:
    async def scenario() -> None:
        registry = SQLiteRegistry()
        queue: WorkQueue[ResourceKey] = WorkQueue()
        manager = _manager(registry, queue=queue)
        await registry.put(_service())
        await registry.put(
            Resource(
                kind="Widget", metadata=ObjectMeta(name="w", namespace=NAMESPACE, tenant_id=TENANT)
            )
        )

        assert await manager.poll_changes() == 2
        assert len(queue) == 1
        key = await queue.get()
        assert key == _key()
        await manager.process(key)
        queue.done(key)

        assert await manager.poll_changes() == 1
        assert len(queue) == 0
        assert await registry.changes_since(0) == []
        await registry.close()

    _run(scenario())


@pytest.mark.unit
def test_requeue_after_from_the_reconciler_schedules_the_next_attempt() -> None:
    async def scenario() -> tuple[list[tuple[ResourceKey, float]], list[str]]:
        registry = SQLiteRegistry()
        queue = RecordingQueue()
        await registry.put(_service())
        manager = _manager(registry, policy=DenyPolicy(), queue=queue)
        await manager.process(_key())
        reasons = [condition.reason for condition in await _conditions(registry)]
        await registry.close()
        return queue.delays, reasons

    delays, reasons = _run(scenario())
    assert delays == [(_key(), 30.0)]
    assert reasons == ["PolicyDenied"]


@pytest.mark.unit
def test_failing_reconciles_back_off_per_resource() -> None:
    async def scenario() -> int:
        registry = SQLiteRegistry()
        queue: WorkQueue[ResourceKey] = WorkQueue(base_delay=0.01, max_delay=0.05, jitter=0)
        policy = FailingPolicy()
        await registry.put(_service())
        manager = _manager(registry, policy=policy, queue=queue, poll_interval=0.01)
        await manager.start()
        try:

            async def retried() -> bool:
                return queue.failures(_key()) >= 3

            await _eventually(retried)
        finally:
            await manager.stop()
            await registry.close()
        return policy.calls

    assert _run(scenario()) >= 3


@pytest.mark.unit
def test_an_instance_check_queues_the_managed_resources_it_reports() -> None:
    """核对实例报告的资源加入队列，其他 kind 的资源不加入。"""
    widget = ResourceKey(TENANT, "Widget", NAMESPACE, "w")

    async def check() -> list[ResourceKey]:
        return [_key("a"), widget, _key("b")]

    async def scenario() -> list[ResourceKey]:
        registry = SQLiteRegistry()
        queue = RecordingQueue()
        manager = _manager(registry, queue=queue, instance_check=check)
        await manager.check_instances()
        await registry.close()
        return queue.added

    assert _run(scenario()) == [_key("a"), _key("b")]


@pytest.mark.unit
def test_a_running_manager_checks_instances_periodically_until_it_stops(caplog) -> None:
    """控制器运行时每隔 ``instance_check_interval`` 秒核对一次实例：核对失败时记日志，之后照常
    核对、把报告的资源加入队列；停止之后不再核对。"""
    calls = 0

    async def check() -> list[ResourceKey]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("launcher unavailable")
        return [_key()]

    async def scenario() -> tuple[int, list[ResourceKey]]:
        registry = SQLiteRegistry()
        queue = RecordingQueue()
        manager = _manager(
            registry, queue=queue, instance_check=check, instance_check_interval=0.01
        )
        await manager.start()
        try:

            async def checked_again() -> bool:
                return calls >= 3

            await _eventually(checked_again)
        finally:
            await manager.stop()
        stopped_after = calls
        await asyncio.sleep(0.05)
        await registry.close()
        return stopped_after, queue.added

    stopped_after, added = _run(scenario())
    assert calls == stopped_after
    assert added and set(added) == {_key()}
    [failure] = [r for r in caplog.records if r.getMessage() == "controller instance check failed"]
    assert str(failure.exc_info[1]) == "launcher unavailable"


@pytest.mark.unit
def test_a_manager_reclaims_allocations_when_it_starts_and_after_every_resync(caplog) -> None:
    """启动时回收一次孤立的分配，失败时记日志、照常启动；之后每次周期性全量同步之后再回收
    一次，停止之后不再回收。"""
    calls = 0

    async def reclaim() -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        return []

    async def scenario() -> tuple[int, int]:
        registry = SQLiteRegistry()
        manager = _manager(registry, reclaim=reclaim, resync_period=0.01)
        await manager.start()
        at_start = calls
        try:

            async def reclaimed_again() -> bool:
                return calls >= 3

            await _eventually(reclaimed_again)
        finally:
            await manager.stop()
        stopped_after = calls
        await asyncio.sleep(0.05)
        await registry.close()
        return at_start, stopped_after

    at_start, stopped_after = _run(scenario())
    assert at_start == 1
    assert calls == stopped_after
    [failure] = [
        r for r in caplog.records if r.getMessage() == "controller allocation reclaim failed"
    ]
    assert str(failure.exc_info[1]) == "database is locked"


@pytest.mark.unit
def test_stop_lets_an_in_flight_reconcile_finish() -> None:
    async def scenario() -> list[str]:
        registry = SQLiteRegistry()
        await registry.put(_service())
        policy = GatedPolicy()
        manager = _manager(registry, policy=policy)
        await manager.start()
        await asyncio.wait_for(policy.entered.wait(), 5)

        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()
        policy.release.set()
        await asyncio.wait_for(stopping, 5)

        reasons = [condition.reason for condition in await _conditions(registry)]
        await registry.close()
        return reasons

    assert _run(scenario()) == ["SchedulerUnavailable"]
