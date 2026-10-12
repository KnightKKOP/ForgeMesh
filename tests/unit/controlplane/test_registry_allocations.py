"""分配持久化单元测试：归属资源 uid、设备独占、幂等键与存量库迁移。

覆盖 :meth:`SQLiteRegistry.create_allocation` 及配套的读取 / 释放方法：

* 分配以归属资源 uid 记录，设备写入 ``allocation_devices``；
* 一个幂等键只对应一个分配：重放返回原分配，释放后也不复用；
* 归属资源须存在、uid 一致且不在删除中；
* 同一资源至多一个 Active 分配、同一设备至多属于一个 Active 分配，冲突时不写库；
* 同名资源在不同命名空间 / 租户中各自持有分配；
* 释放后设备回到空闲；约束由数据库强制（直接写库、独立连接并发、重启后依然成立）；
* 打开存量库时以按租户的唯一索引取代旧索引；
* 持有 Active 分配的资源无论处于哪个 phase，删除都先进入 ``Terminating``。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator

import pytest
from forgemesh.controlplane.registry import DEFAULT_RELEASE_FINALIZER, SQLiteRegistry
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.allocation import Allocation
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

KIND = "InferenceService"
TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
GPU0 = AllocatedDevice(node_id="node-a", device_id="gpu-0")
GPU1 = AllocatedDevice(node_id="node-a", device_id="gpu-1")


def _service(
    name: str = "svc",
    namespace: str | None = "team-a",
    uid: str = "uid-1",
    tenant_id: str = TENANT,
    phase: InferencePhase = InferencePhase.PENDING,
) -> InferenceService:
    service = InferenceService(
        metadata=ObjectMeta(name=name, namespace=namespace, uid=uid, tenant_id=tenant_id),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
    )
    service.status.phase = phase
    return service


def _ref(service: InferenceService) -> ResourceRef:
    meta = service.metadata
    return ResourceRef(kind=service.kind, namespace=meta.namespace, name=meta.name, uid=meta.uid)


def _put(registry: SQLiteRegistry, **kwargs) -> ResourceRef:
    """创建一个推理服务并返回它的资源引用。"""
    service = _service(**kwargs)
    asyncio.run(registry.put(service))
    return _ref(service)


@pytest.fixture
def registry() -> Iterator[SQLiteRegistry]:
    reg = SQLiteRegistry()
    yield reg
    asyncio.run(reg.close())


@pytest.mark.unit
def test_allocation_is_recorded_against_the_owner_uid_with_its_devices(registry):
    owner = _put(registry)

    record = asyncio.run(registry.create_allocation(TENANT, owner, "uid-1:1", [GPU1, GPU0]))

    assert record["resource_id"] == "uid-1"
    assert record["status"] == "Active"
    assert record["idempotency_key"] == "uid-1:1"
    assert record["devices"] == [
        {"node_id": "node-a", "device_id": "gpu-1"},
        {"node_id": "node-a", "device_id": "gpu-0"},
    ]
    assert asyncio.run(registry.get_active_allocation("uid-1", TENANT)) == record
    assert asyncio.run(registry.list_allocations(TENANT)) == [record]
    assert asyncio.run(registry.allocated_devices()) == {GPU0, GPU1}


@pytest.mark.unit
def test_replaying_an_idempotency_key_returns_the_same_allocation(registry):
    owner = _put(registry)
    first = asyncio.run(registry.create_allocation(TENANT, owner, "uid-1:1", [GPU0]))

    replay = asyncio.run(registry.create_allocation(TENANT, owner, "uid-1:1", [GPU1]))

    assert replay == first
    assert len(asyncio.run(registry.list_allocations(TENANT))) == 1
    assert asyncio.run(registry.allocated_devices()) == {GPU0}


@pytest.mark.unit
def test_an_idempotency_key_is_never_reused(registry):
    """其他资源、其他租户使用同一键冲突；分配释放后，同一资源也不能再用这个键。"""
    owner = _put(registry)
    other = _put(registry, name="svc-2", uid="uid-2")
    foreign = _put(registry, uid="uid-3", tenant_id=OTHER_TENANT)
    record = asyncio.run(registry.create_allocation(TENANT, owner, "key-1"))

    for tenant_id, ref in ((TENANT, other), (OTHER_TENANT, foreign)):
        with pytest.raises(ConflictError, match="idempotency_key"):
            asyncio.run(registry.create_allocation(tenant_id, ref, "key-1"))
    asyncio.run(registry.release_allocation(record["id"], TENANT))
    with pytest.raises(ConflictError, match="idempotency_key"):
        asyncio.run(registry.create_allocation(TENANT, owner, "key-1"))


@pytest.mark.unit
def test_allocation_requires_the_live_owner(registry):
    """归属资源不存在、uid 不一致（原资源已删除）、属于其他租户或正在删除时不分配。"""
    owner = _put(registry)
    leaving = _service(name="leaving", uid="uid-leaving")
    leaving.finalizers = ["example.com/hold"]
    asyncio.run(registry.put(leaving))
    asyncio.run(registry.request_delete(KIND, "team-a", "leaving", TENANT))

    for tenant_id, ref in (
        (TENANT, ResourceRef(kind=KIND, namespace="team-a", name="ghost", uid="uid-ghost")),
        (TENANT, ResourceRef(kind=KIND, namespace="team-a", name="svc", uid="uid-old")),
        (TENANT, ResourceRef(kind=KIND, namespace=None, name="svc", uid="uid-1")),
        (OTHER_TENANT, owner),
    ):
        with pytest.raises(NotFoundError):
            asyncio.run(registry.create_allocation(tenant_id, ref, f"key-{ref.uid}", [GPU0]))
    with pytest.raises(ConflictError, match="terminating"):
        asyncio.run(registry.create_allocation(TENANT, _ref(leaving), "key-leaving", [GPU0]))

    assert asyncio.run(registry.list_allocations(TENANT)) == []
    assert asyncio.run(registry.allocated_devices()) == frozenset()


@pytest.mark.unit
def test_a_resource_holds_at_most_one_active_allocation(registry):
    owner = _put(registry)
    first = asyncio.run(registry.create_allocation(TENANT, owner, "uid-1:1", [GPU0]))

    with pytest.raises(ConflictError, match="active allocation"):
        asyncio.run(registry.create_allocation(TENANT, owner, "uid-1:2", [GPU1]))
    assert asyncio.run(registry.allocated_devices()) == {GPU0}

    asyncio.run(registry.release_allocation(first["id"], TENANT))
    second = asyncio.run(registry.create_allocation(TENANT, owner, "uid-1:2", [GPU1]))
    assert second["id"] != first["id"]
    assert asyncio.run(registry.allocated_devices()) == {GPU1}


@pytest.mark.unit
def test_a_device_belongs_to_at_most_one_active_allocation(registry):
    """设备已被占用时整个请求被拒绝（不写分配行，也不占用其余设备）；释放后可再分配。"""
    first = asyncio.run(registry.create_allocation(TENANT, _put(registry), "key-1", [GPU0]))
    other = _put(registry, name="svc-2", uid="uid-2")

    with pytest.raises(ConflictError, match="device already allocated"):
        asyncio.run(registry.create_allocation(TENANT, other, "key-2", [GPU1, GPU0]))
    assert asyncio.run(registry.get_active_allocation("uid-2", TENANT)) is None
    assert asyncio.run(registry.allocated_devices()) == {GPU0}

    asyncio.run(registry.release_allocation(first["id"], TENANT))
    assert asyncio.run(registry.allocated_devices()) == frozenset()
    asyncio.run(registry.create_allocation(TENANT, other, "key-2", [GPU1, GPU0]))
    assert asyncio.run(registry.allocated_devices()) == {GPU0, GPU1}
    released = asyncio.run(registry.get_allocation(first["id"]))
    assert released["status"] == "Released"
    assert released["devices"] == [{"node_id": "node-a", "device_id": "gpu-0"}]


@pytest.mark.unit
def test_same_name_in_other_namespaces_and_tenants_allocates_independently(registry):
    owners = [
        (TENANT, _put(registry, namespace="team-a", uid="uid-a")),
        (TENANT, _put(registry, namespace="team-b", uid="uid-b")),
        (TENANT, _put(registry, namespace=None, uid="uid-c")),
        (OTHER_TENANT, _put(registry, namespace="team-a", uid="uid-d", tenant_id=OTHER_TENANT)),
    ]

    for index, (tenant_id, owner) in enumerate(owners):
        device = AllocatedDevice(node_id="node-a", device_id=f"gpu-{index}")
        asyncio.run(registry.create_allocation(tenant_id, owner, f"{owner.uid}:1", [device]))

    for tenant_id, owner in owners:
        active = asyncio.run(registry.get_active_allocation(owner.uid, tenant_id))
        assert active is not None and active["resource_id"] == owner.uid
    assert asyncio.run(registry.get_active_allocation("uid-d", TENANT)) is None
    assert len(asyncio.run(registry.allocated_devices())) == 4


@pytest.mark.unit
def test_release_active_allocation_releases_by_owner_uid_within_the_tenant(registry):
    record = asyncio.run(registry.create_allocation(TENANT, _put(registry), "key-1", [GPU0]))

    assert asyncio.run(registry.release_active_allocation("uid-1", OTHER_TENANT)) is None
    assert asyncio.run(registry.get_active_allocation("uid-1", TENANT)) is not None

    released_id = asyncio.run(registry.release_active_allocation("uid-1", TENANT, reason="test"))
    assert released_id == record["id"]
    assert asyncio.run(registry.release_active_allocation("uid-1", TENANT)) is None
    assert asyncio.run(registry.allocated_devices()) == frozenset()
    [released] = asyncio.run(registry.list_allocations(TENANT))
    assert released["status"] == "Released"
    assert released["released_at"] is not None


@pytest.mark.unit
def test_allocation_lookups_by_id_and_idempotency_key(registry):
    owner = _put(registry, uid="uid-x", tenant_id=OTHER_TENANT)
    record = asyncio.run(registry.create_allocation(OTHER_TENANT, owner, "key-x", [GPU0]))

    assert asyncio.run(registry.get_allocation(record["id"])) == record
    assert asyncio.run(registry.get_allocation("no-such-id")) is None
    assert asyncio.run(registry.get_allocation_by_idempotency_key("key-x", OTHER_TENANT)) == record
    assert asyncio.run(registry.get_allocation_by_idempotency_key("key-x", TENANT)) is None


@pytest.mark.unit
def test_duplicate_devices_in_one_request_are_rejected(registry):
    with pytest.raises(ValueError, match="duplicate devices"):
        asyncio.run(registry.create_allocation(TENANT, _put(registry), "key-1", [GPU0, GPU0]))


@pytest.mark.unit
def test_the_database_enforces_exclusivity_without_the_registry_checks(registry):
    """绕过 create_allocation 直接写库：设备重复占用、同一资源两个 Active 均被约束拒绝；
    分配状态改变时设备随之释放（如租约过期）。"""
    first = asyncio.run(registry.create_allocation(TENANT, _put(registry), "key-1", [GPU0]))
    conn = registry._conn
    insert_allocation = (
        "INSERT INTO allocations (id, tenant_id, resource_id, status) VALUES (?, ?, ?, 'Active')"
    )
    insert_device = (
        "INSERT INTO allocation_devices (allocation_id, node_id, device_id, status) "
        "VALUES (?, 'node-a', 'gpu-0', 'Active')"
    )

    conn.execute(insert_allocation, ("raw", TENANT, "uid-raw"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_device, ("raw",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_allocation, ("raw-2", TENANT, "uid-1"))
    conn.execute(insert_allocation, ("raw-3", OTHER_TENANT, "uid-1"))
    conn.rollback()

    conn.execute("UPDATE allocations SET status = 'Expired' WHERE id = ?", (first["id"],))
    conn.commit()
    assert asyncio.run(registry.allocated_devices()) == frozenset()


@pytest.mark.unit
@pytest.mark.parametrize(
    "phase",
    [
        InferencePhase.PENDING,
        InferencePhase.SCHEDULING,
        InferencePhase.PREPARING,
        InferencePhase.FAILED,
        InferencePhase.UNKNOWN,
    ],
)
def test_deleting_a_resource_that_holds_an_allocation_goes_through_terminating(registry, phase):
    """没有外部副作用的 phase 也要先释放分配：资源进入 Terminating，分配留给协调器释放。"""
    owner = _put(registry, phase=phase)
    asyncio.run(registry.create_allocation(TENANT, owner, "uid-1:1", [GPU0]))

    result = asyncio.run(registry.request_delete(KIND, "team-a", "svc", TENANT))

    assert result.deleted is False
    assert result.finalizers == [DEFAULT_RELEASE_FINALIZER]
    _, stored_phase = asyncio.run(registry.get_with_phase(KIND, "team-a", "svc", TENANT))
    assert stored_phase == "Terminating"
    assert asyncio.run(registry.get_active_allocation("uid-1", TENANT)) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "phase", [InferencePhase.PENDING, InferencePhase.SCHEDULING, InferencePhase.UNKNOWN]
)
def test_deleting_a_resource_without_an_active_allocation_is_immediate(registry, phase):
    """从未分配、或分配已释放的无副作用资源仍直接物理删除。"""
    released = _put(registry, phase=phase)
    record = asyncio.run(registry.create_allocation(TENANT, released, "uid-1:1", [GPU0]))
    asyncio.run(registry.release_allocation(record["id"], TENANT))
    _put(registry, name="svc-2", uid="uid-2", phase=phase)

    for name in ("svc", "svc-2"):
        assert asyncio.run(registry.request_delete(KIND, "team-a", name, TENANT)).deleted is True


@pytest.mark.unit
def test_opening_a_legacy_database_replaces_the_global_active_index(tmp_path):
    db_file = tmp_path / "legacy.db"
    asyncio.run(SQLiteRegistry(str(db_file)).close())
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        DROP TRIGGER trg_allocation_devices_follow_status;
        DROP TABLE allocation_devices;
        DROP INDEX idx_allocations_one_active_per_tenant_resource;
        CREATE UNIQUE INDEX idx_allocations_one_active_per_resource
            ON allocations(resource_id) WHERE status = 'Active';
        INSERT INTO allocations (id, tenant_id, resource_id, status, idempotency_key)
            VALUES ('legacy', 'tenant-a', 'InferenceService/svc', 'Active', 'legacy-key');
        """
    )
    conn.close()

    reg = SQLiteRegistry(str(db_file))
    try:
        names = {row["name"] for row in reg._conn.execute("SELECT name FROM sqlite_master")}
        assert "idx_allocations_one_active_per_resource" not in names
        assert {
            "idx_allocations_one_active_per_tenant_resource",
            "allocation_devices",
            "idx_allocation_devices_one_active",
            "trg_allocation_devices_follow_status",
        } <= names
        [legacy] = asyncio.run(reg.list_allocations(TENANT))
        assert (legacy["id"], legacy["resource_id"], legacy["devices"]) == (
            "legacy",
            "InferenceService/svc",
            [],
        )
        asyncio.run(
            reg.put_allocation(
                Allocation(
                    resource_id="InferenceService/svc",
                    tenant_id=OTHER_TENANT,
                    idempotency_key="other-tenant",
                )
            )
        )
    finally:
        asyncio.run(reg.close())


@pytest.mark.unit
def test_allocations_and_device_claims_survive_a_restart(tmp_path):
    db = str(tmp_path / "registry.db")
    reg = SQLiteRegistry(db)
    owner = _put(reg)
    record = asyncio.run(reg.create_allocation(TENANT, owner, "uid-1:1", [GPU0]))
    asyncio.run(reg.close())

    reopened = SQLiteRegistry(db)
    try:
        assert asyncio.run(reopened.create_allocation(TENANT, owner, "uid-1:1", [GPU1])) == record
        assert asyncio.run(reopened.allocated_devices()) == {GPU0}
        other = _put(reopened, name="svc-2", uid="uid-2")
        with pytest.raises(ConflictError, match="device already allocated"):
            asyncio.run(reopened.create_allocation(TENANT, other, "uid-2:1", [GPU0]))
    finally:
        asyncio.run(reopened.close())


@pytest.mark.unit
@pytest.mark.parametrize("connections", [1, 2], ids=["shared-registry", "two-connections"])
def test_concurrent_allocations_never_double_book_a_device(tmp_path, connections):
    db = str(tmp_path / "registry.db")

    async def scenario() -> None:
        registries = [SQLiteRegistry(db) for _ in range(connections)]
        try:
            owners = []
            for index in range(8):
                service = _service(name=f"svc-{index}", uid=f"uid-{index}")
                await registries[0].put(service)
                owners.append(_ref(service))

            results = await asyncio.gather(
                *(
                    registries[index % connections].create_allocation(
                        TENANT, owner, f"{owner.uid}:1", [GPU0]
                    )
                    for index, owner in enumerate(owners)
                ),
                return_exceptions=True,
            )

            winners = [result for result in results if isinstance(result, dict)]
            losers = [result for result in results if not isinstance(result, dict)]
            assert len(winners) == 1
            assert all(isinstance(loser, ConflictError) for loser in losers)
            assert await registries[-1].allocated_devices() == {GPU0}
            assert len(await registries[-1].list_allocations(TENANT)) == 1
        finally:
            for reg in registries:
                await reg.close()

    asyncio.run(scenario())
