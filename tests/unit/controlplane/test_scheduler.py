"""首次适配调度器单元测试（:class:`FirstFitScheduler` + :class:`SQLiteRegistry`）。

覆盖：

* 按清单顺序挑选类型精确匹配、显存足够的空闲设备，不跨节点凑数；容量不足时不写库；
* 同一幂等键返回同一分配（含重启后、并发重放）；已释放分配的键不复用；
* 资源已持有满足需求的分配时复用，不满足时报冲突；
* ``feasible()`` 只读；``release()`` 幂等并释放设备；
* 并发分配不重复占用设备，同一进程内的并发分配能用满空闲设备；
* 同名资源在不同命名空间 / 租户中各自持有分配；
* 非法需求、没有加速器需求、归属资源不存在或正在删除。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.controlplane.scheduler import (
    FirstFitScheduler,
    InsufficientCapacityError,
    Inventory,
)
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta
from forgemesh.core.scheduler import (
    AllocatedDevice,
    AllocationRef,
    ResourceRef,
    ScheduleRequest,
)
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferenceService,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

KIND = "InferenceService"
TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


def _gpu(device_id: str, memory: int = 80, accelerator: str = "nvidia-gpu") -> dict[str, Any]:
    return {"id": device_id, "type": accelerator, "memoryGiB": memory}


def _inventory(*nodes: tuple[str, list[dict[str, Any]]]) -> Inventory:
    document = {"nodes": [{"id": node_id, "devices": devices} for node_id, devices in nodes]}
    return Inventory.model_validate_json(json.dumps(document))


#: node-a：一张小显存卡、一张大显存卡、一张其他类型的卡；node-b：两张大显存卡。
INVENTORY = _inventory(
    ("node-a", [_gpu("gpu-0", memory=24), _gpu("gpu-1"), _gpu("gpu-2", accelerator="amd-gpu")]),
    ("node-b", [_gpu("gpu-0"), _gpu("gpu-1")]),
)

#: 两个节点各两张同规格的卡。
FOUR_GPUS = _inventory(
    ("node-a", [_gpu("gpu-0"), _gpu("gpu-1")]), ("node-b", [_gpu("gpu-0"), _gpu("gpu-1")])
)


def _device(node_id: str, device_id: str) -> AllocatedDevice:
    return AllocatedDevice(node_id=node_id, device_id=device_id)


def _service(
    name: str = "svc",
    namespace: str | None = "team-a",
    uid: str = "uid-1",
    tenant_id: str = TENANT,
) -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(name=name, namespace=namespace, uid=uid, tenant_id=tenant_id),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
    )


def _ref(service: InferenceService) -> ResourceRef:
    meta = service.metadata
    return ResourceRef(kind=service.kind, namespace=meta.namespace, name=meta.name, uid=meta.uid)


def _put(registry: SQLiteRegistry, name: str = "svc", **kwargs: Any) -> ResourceRef:
    """创建一个推理服务并返回它的资源引用。"""
    service = _service(name, **kwargs)
    asyncio.run(registry.put(service))
    return _ref(service)


def _request(
    owner: ResourceRef,
    key: str | None = None,
    *,
    tenant_id: str = TENANT,
    accelerator: str = "nvidia-gpu",
    count: int = 1,
    memory: int = 16,
) -> ScheduleRequest:
    return ScheduleRequest(
        tenant_id=tenant_id,
        resource_ref=owner,
        idempotency_key=key or f"{owner.uid}:1",
        requirements={"accelerator": {"type": accelerator, "count": count, "minMemoryGiB": memory}},
    )


@pytest.fixture
def registry() -> Iterator[SQLiteRegistry]:
    reg = SQLiteRegistry()
    yield reg
    asyncio.run(reg.close())


@pytest.fixture
def scheduler(registry) -> FirstFitScheduler:
    return FirstFitScheduler(registry, INVENTORY)


@pytest.mark.unit
def test_first_fit_takes_matching_free_devices_in_inventory_order(registry, scheduler):
    """类型精确匹配、显存不低于下限；同一节点凑不齐数量时整体换到下一个节点。"""
    big = asyncio.run(scheduler.allocate(_request(_put(registry, uid="uid-big"), memory=40)))
    pair = asyncio.run(scheduler.allocate(_request(_put(registry, "pair", uid="uid-p"), count=2)))
    amd = asyncio.run(
        scheduler.allocate(_request(_put(registry, "amd", uid="uid-amd"), accelerator="amd-gpu"))
    )
    small = asyncio.run(scheduler.allocate(_request(_put(registry, "small", uid="uid-s"))))

    assert big.devices == [_device("node-a", "gpu-1")]
    assert pair.devices == [_device("node-b", "gpu-0"), _device("node-b", "gpu-1")]
    assert amd.devices == [_device("node-a", "gpu-2")]
    assert small.devices == [_device("node-a", "gpu-0")]
    assert (big.resource_id, big.lease_until) == ("uid-big", None)
    stored = asyncio.run(registry.get_active_allocation("uid-p", TENANT))
    assert stored is not None and stored["id"] == pair.allocation_id
    assert stored["devices"] == [
        {"node_id": "node-b", "device_id": "gpu-0"},
        {"node_id": "node-b", "device_id": "gpu-1"},
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "requirements",
    [
        {"count": 3},
        {"memory": 81},
        {"accelerator": "tpu"},
        {"accelerator": "amd-gpu", "count": 2},
    ],
    ids=["count-beyond-any-node", "memory", "type", "count-of-type"],
)
def test_insufficient_capacity_raises_and_writes_nothing(registry, scheduler, requirements):
    owner = _put(registry)

    with pytest.raises(InsufficientCapacityError, match="no node has"):
        asyncio.run(scheduler.allocate(_request(owner, **requirements)))

    assert asyncio.run(registry.list_allocations(TENANT)) == []
    assert asyncio.run(registry.allocated_devices()) == frozenset()


@pytest.mark.unit
def test_devices_held_by_active_allocations_are_skipped_until_released(registry):
    scheduler = FirstFitScheduler(registry, _inventory(("node-a", [_gpu("gpu-0")])))
    first = asyncio.run(scheduler.allocate(_request(_put(registry))))
    other = _put(registry, "other", uid="uid-2")

    with pytest.raises(InsufficientCapacityError):
        asyncio.run(scheduler.allocate(_request(other)))
    asyncio.run(scheduler.release(first.allocation_id))
    second = asyncio.run(scheduler.allocate(_request(other)))

    assert second.devices == first.devices == [_device("node-a", "gpu-0")]


@pytest.mark.unit
def test_replaying_a_key_returns_the_same_allocation(registry, scheduler):
    owner = _put(registry)
    first = asyncio.run(scheduler.allocate(_request(owner)))

    replay = asyncio.run(scheduler.allocate(_request(owner)))

    assert replay == first
    assert len(asyncio.run(registry.list_allocations(TENANT))) == 1
    assert asyncio.run(registry.allocated_devices()) == set(first.devices)


@pytest.mark.unit
def test_replaying_a_key_after_a_restart_returns_the_same_allocation(tmp_path):
    db = str(tmp_path / "registry.db")
    registry = SQLiteRegistry(db)
    owner = _put(registry)
    first = asyncio.run(FirstFitScheduler(registry, INVENTORY).allocate(_request(owner)))
    asyncio.run(registry.close())

    reopened = SQLiteRegistry(db)
    try:
        scheduler = FirstFitScheduler(reopened, INVENTORY)
        replay = asyncio.run(scheduler.allocate(_request(owner)))
        other = asyncio.run(scheduler.allocate(_request(_put(reopened, "other", uid="uid-2"))))

        assert replay == first
        assert len(asyncio.run(reopened.list_allocations(TENANT))) == 2
        assert other.devices != first.devices
    finally:
        asyncio.run(reopened.close())


@pytest.mark.unit
def test_a_new_generation_reuses_an_allocation_that_still_fits(registry, scheduler):
    owner = _put(registry)
    first = asyncio.run(scheduler.allocate(_request(owner, "uid-1:1", memory=40)))

    second = asyncio.run(scheduler.allocate(_request(owner, "uid-1:2", memory=60)))

    assert second == first
    assert len(asyncio.run(registry.list_allocations(TENANT))) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "requirements",
    [{"count": 2}, {"memory": 40}, {"accelerator": "amd-gpu"}],
    ids=["count", "memory", "type"],
)
def test_a_new_generation_that_no_longer_fits_must_release_first(registry, scheduler, requirements):
    owner = _put(registry)
    first = asyncio.run(scheduler.allocate(_request(owner, "uid-1:1")))
    assert first.devices == [_device("node-a", "gpu-0")]

    with pytest.raises(ConflictError, match="must be released first"):
        asyncio.run(scheduler.allocate(_request(owner, "uid-1:2", **requirements)))
    assert asyncio.run(registry.allocated_devices()) == set(first.devices)

    asyncio.run(scheduler.release(first.allocation_id))
    second = asyncio.run(scheduler.allocate(_request(owner, "uid-1:2", **requirements)))
    assert second.allocation_id != first.allocation_id


@pytest.mark.unit
def test_a_released_key_is_not_reused(registry, scheduler):
    owner = _put(registry)
    first = asyncio.run(scheduler.allocate(_request(owner, "uid-1:1")))
    asyncio.run(scheduler.release(first.allocation_id))

    with pytest.raises(ConflictError, match="Released"):
        asyncio.run(scheduler.allocate(_request(owner, "uid-1:1")))
    retried = asyncio.run(scheduler.allocate(_request(owner, "uid-1:1:retry")))

    assert retried.allocation_id != first.allocation_id
    assert retried.devices == first.devices


@pytest.mark.unit
def test_a_key_of_another_resource_is_rejected(registry, scheduler):
    owner = _put(registry)
    asyncio.run(scheduler.allocate(_request(owner, "shared-key")))
    other = _put(registry, "other", uid="uid-2")

    with pytest.raises(ConflictError, match="belongs to another resource"):
        asyncio.run(scheduler.allocate(_request(other, "shared-key")))
    assert asyncio.run(registry.get_active_allocation("uid-2", TENANT)) is None


@pytest.mark.unit
def test_feasible_reports_without_writing(registry, scheduler):
    owner = _put(registry)
    other = _put(registry, "other", uid="uid-2")
    changes = registry._conn.total_changes

    fresh = asyncio.run(scheduler.feasible(_request(owner, memory=40)))
    assert (fresh.feasible, fresh.candidates) == (True, ["node-a/gpu-1"])
    assert registry._conn.total_changes == changes
    assert asyncio.run(registry.list_allocations(TENANT)) == []

    allocation = asyncio.run(scheduler.allocate(_request(owner, memory=40)))
    changes = registry._conn.total_changes
    results = {
        "served": asyncio.run(scheduler.feasible(_request(owner, memory=40))),
        "next": asyncio.run(scheduler.feasible(_request(other, memory=40))),
        "too-many": asyncio.run(scheduler.feasible(_request(other, count=3))),
        "mismatch": asyncio.run(scheduler.feasible(_request(owner, "uid-1:2", count=2))),
        "foreign-key": asyncio.run(scheduler.feasible(_request(other, "uid-1:1"))),
    }

    assert registry._conn.total_changes == changes
    assert results["served"].feasible is True
    assert allocation.allocation_id in results["served"].reason
    assert results["served"].candidates == ["node-a/gpu-1"]
    assert (results["next"].feasible, results["next"].candidates) == (True, ["node-b/gpu-0"])
    assert results["too-many"].feasible is False
    assert "no node has 3 free nvidia-gpu device(s)" in results["too-many"].reason
    assert results["mismatch"].feasible is False
    assert "must be released first" in results["mismatch"].reason
    assert results["foreign-key"].feasible is False
    assert "belongs to another resource" in results["foreign-key"].reason


@pytest.mark.unit
def test_release_frees_the_devices_and_is_idempotent(registry, scheduler):
    allocation = asyncio.run(scheduler.allocate(_request(_put(registry), count=2)))

    asyncio.run(scheduler.release(allocation.allocation_id))
    asyncio.run(scheduler.release(allocation.allocation_id))

    assert asyncio.run(registry.allocated_devices()) == frozenset()
    released = asyncio.run(registry.get_allocation(allocation.allocation_id))
    assert released["status"] == "Released"
    assert asyncio.run(registry.get_active_allocation("uid-1", TENANT)) is None
    with pytest.raises(NotFoundError):
        asyncio.run(scheduler.release("no-such-allocation"))


@pytest.mark.unit
def test_concurrent_releases_of_one_allocation_both_succeed(registry, scheduler):
    allocation = asyncio.run(scheduler.allocate(_request(_put(registry))))

    async def release_twice() -> None:
        await asyncio.gather(
            scheduler.release(allocation.allocation_id),
            scheduler.release(allocation.allocation_id),
        )

    asyncio.run(release_twice())

    assert asyncio.run(registry.get_allocation(allocation.allocation_id))["status"] == "Released"


class _InlineRegistry(SQLiteRegistry):
    """在事件循环线程内执行数据库操作，每次操作后让出一次：并发协程的交错顺序固定。"""

    async def _run(self, fn: Callable[[], Any]) -> Any:
        with self._lock:
            result = fn()
        await asyncio.sleep(0)
        return result


async def _allocate_concurrently(registries: list[SQLiteRegistry], requests: int) -> list[Any]:
    """为 ``requests`` 个资源各申请一张卡，轮流交给各注册表上的调度器并发执行。"""
    schedulers = [FirstFitScheduler(reg, FOUR_GPUS) for reg in registries]
    owners = []
    for index in range(requests):
        service = _service(f"svc-{index}", uid=f"uid-{index}")
        await registries[0].put(service)
        owners.append(_ref(service))
    return await asyncio.gather(
        *(
            schedulers[index % len(schedulers)].allocate(_request(owner))
            for index, owner in enumerate(owners)
        ),
        return_exceptions=True,
    )


@pytest.mark.unit
def test_concurrent_allocations_in_one_process_use_every_free_device():
    """6 个单卡请求争抢 4 张卡：恰好 4 个成功且各占一张，其余容量不足。

    首次适配下并发请求会挑中同一张卡：调度器内不串行化时，落败方耗尽重试后报冲突，
    卡却仍有空闲。``_InlineRegistry`` 固定交错顺序，使结果可复现。
    """

    async def scenario() -> tuple[list[Any], frozenset[AllocatedDevice]]:
        registry = _InlineRegistry()
        try:
            results = await _allocate_concurrently([registry], 6)
            return results, await registry.allocated_devices()
        finally:
            await registry.close()

    results, allocated = asyncio.run(scenario())

    winners = [result for result in results if isinstance(result, AllocationRef)]
    losers = [result for result in results if not isinstance(result, AllocationRef)]
    assert len(winners) == len(allocated) == 4
    assert {device for winner in winners for device in winner.devices} == allocated
    assert len(losers) == 2
    assert all(isinstance(loser, InsufficientCapacityError) for loser in losers)


@pytest.mark.unit
@pytest.mark.parametrize("connections", [1, 2], ids=["shared-registry", "two-connections"])
def test_concurrent_allocations_never_double_book_a_device(tmp_path, connections):
    """工作线程中真实并发（含两个连接上各自的调度器）：成功的分配之间不共享设备，
    Active 分配与占用的设备一一对应，失败只有容量不足或冲突两种。"""
    db = str(tmp_path / "registry.db")

    async def scenario() -> tuple[list[Any], list[dict[str, Any]], frozenset[AllocatedDevice]]:
        registries = [SQLiteRegistry(db) for _ in range(connections)]
        try:
            results = await _allocate_concurrently(registries, 6)
            records = await registries[-1].list_allocations(TENANT)
            return results, records, await registries[-1].allocated_devices()
        finally:
            for reg in registries:
                await reg.close()

    results, records, allocated = asyncio.run(scenario())

    winners = [result for result in results if isinstance(result, AllocationRef)]
    losers = [result for result in results if not isinstance(result, AllocationRef)]
    claimed = [device for winner in winners for device in winner.devices]
    assert len(claimed) == len(set(claimed)) == len(winners) >= 1
    assert set(claimed) == allocated
    active_ids = sorted(record["id"] for record in records if record["status"] == "Active")
    assert active_ids == sorted(winner.allocation_id for winner in winners)
    assert all(isinstance(loser, (InsufficientCapacityError, ConflictError)) for loser in losers)


@pytest.mark.unit
def test_concurrent_replays_of_one_key_share_one_allocation(registry):
    owner = _put(registry)

    async def replay() -> list[AllocationRef]:
        scheduler = FirstFitScheduler(registry, INVENTORY)
        return await asyncio.gather(*(scheduler.allocate(_request(owner)) for _ in range(5)))

    results = asyncio.run(replay())

    assert len({result.allocation_id for result in results}) == 1
    assert len(asyncio.run(registry.list_allocations(TENANT))) == 1


@pytest.mark.unit
def test_same_name_in_other_namespaces_and_tenants_holds_independent_allocations(
    registry, scheduler
):
    owners = [
        (TENANT, _put(registry, namespace="team-a", uid="uid-a")),
        (TENANT, _put(registry, namespace="team-b", uid="uid-b")),
        (TENANT, _put(registry, namespace=None, uid="uid-c")),
        (OTHER_TENANT, _put(registry, namespace="team-a", uid="uid-d", tenant_id=OTHER_TENANT)),
    ]

    allocations = [
        asyncio.run(scheduler.allocate(_request(owner, tenant_id=tenant_id)))
        for tenant_id, owner in owners
    ]
    asyncio.run(scheduler.release(allocations[0].allocation_id))

    assert len({device for allocation in allocations for device in allocation.devices}) == 4
    assert asyncio.run(registry.get_active_allocation("uid-a", TENANT)) is None
    for tenant_id, owner in owners[1:]:
        active = asyncio.run(registry.get_active_allocation(owner.uid, tenant_id))
        assert active is not None and active["resource_id"] == owner.uid
    assert asyncio.run(registry.get_active_allocation("uid-d", TENANT)) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "requirements",
    [
        {"accelerator": {"type": "nvidia-gpu", "count": 0, "minMemoryGiB": 16}},
        {"accelerator": {"type": "nvidia-gpu"}},
        {"accelerator": "nvidia-gpu"},
        {"gpu": 1},
    ],
    ids=["zero-count", "no-memory", "not-an-object", "unknown-key"],
)
def test_invalid_requirements_are_rejected(registry, scheduler, requirements):
    request = _request(_put(registry)).model_copy(update={"requirements": requirements})

    with pytest.raises(ValueError, match="invalid scheduling requirements"):
        asyncio.run(scheduler.feasible(request))
    with pytest.raises(ValueError, match="invalid scheduling requirements"):
        asyncio.run(scheduler.allocate(request))
    assert asyncio.run(registry.list_allocations(TENANT)) == []


@pytest.mark.unit
@pytest.mark.parametrize("requirements", [{}, {"accelerator": None}], ids=["empty", "null"])
def test_a_request_without_an_accelerator_gets_an_allocation_without_devices(
    registry, scheduler, requirements
):
    owner = _put(registry)
    request = _request(owner).model_copy(update={"requirements": requirements})

    allocation = asyncio.run(scheduler.allocate(request))
    next_generation = request.model_copy(update={"idempotency_key": "uid-1:2"})

    assert allocation.devices == []
    assert asyncio.run(scheduler.feasible(request)).feasible is True
    assert asyncio.run(scheduler.allocate(next_generation)) == allocation
    assert asyncio.run(registry.allocated_devices()) == frozenset()


@pytest.mark.unit
def test_allocation_requires_a_live_owner(registry, scheduler):
    ghost = ResourceRef(kind=KIND, namespace="team-a", name="ghost", uid="uid-ghost")
    with pytest.raises(NotFoundError):
        asyncio.run(scheduler.allocate(_request(ghost)))

    leaving = _put(registry, "leaving", uid="uid-leaving")
    current = asyncio.run(registry.get(KIND, "team-a", "leaving", TENANT))
    current.finalizers = ["example.com/hold"]
    asyncio.run(registry.put(current, expected_version=current.metadata.resourceVersion))
    asyncio.run(registry.request_delete(KIND, "team-a", "leaving", TENANT))
    with pytest.raises(ConflictError, match="terminating"):
        asyncio.run(scheduler.allocate(_request(leaving)))

    assert asyncio.run(registry.list_allocations(TENANT)) == []
    assert asyncio.run(registry.allocated_devices()) == frozenset()
