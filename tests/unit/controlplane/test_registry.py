"""``forgemesh.controlplane.registry`` 单元测试。

覆盖 SQLiteRegistry 契约实现的核心语义：

* Create / Get 往返；
* 乐观并发 CAS（成功 / 冲突）；
* 重复 Create 抛 ConflictError；
* Delete（成功 / NotFound）；
* 租户隔离（跨租户 Get 抛 NotFoundError）；
* 分页游标（5 条分 3 页取完；签名游标绑定租户 / kind / 过滤条件、防篡改、重启后有效）；
* namespace 过滤；
* generation 自增；
* resource_version 递增；
* labels 过滤、watch 未实现、Schema 校验；
* Allocation 持久化（部分唯一索引 / 幂等键 / 租户隔离 / 状态机释放）；
* request_delete 受控删除（finalizer / Terminating / 向后兼容）；
* put() 状态机接线（非法跳变拒绝 / 合法路径通过）；
* 存量库幂等迁移（phase / finalizers 列补齐）。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
from collections.abc import Callable

import pytest
from forgemesh.controlplane.registry import (
    DEFAULT_RELEASE_FINALIZER,
    InvalidPageTokenError,
    SQLiteRegistry,
)
from forgemesh.controlplane.registry.models import bump_generation, validate_resource
from forgemesh.core.errors import ConflictError, InvalidStateTransition, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.resource.allocation import Allocation
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

KIND = "InferenceService"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _make_resource(
    name: str = "demo",
    namespace: str = "team-a",
    tenant_id: str = TENANT_A,
    kind: str = KIND,
    generation: int = 0,
    uid: str = "uid-1",
) -> Resource:
    """构造一个可复用的最小 Resource。"""
    return Resource(
        kind=kind,
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            uid=uid,
            tenant_id=tenant_id,
            generation=generation,
        ),
    )


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# Create / Get
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_create_and_get() -> None:
    reg = SQLiteRegistry()
    res = _make_resource()

    created = _run(reg.put(res))
    assert created.metadata.resourceVersion == "0"

    got = _run(reg.get(KIND, "team-a", "demo", TENANT_A))
    assert got.metadata.name == "demo"
    assert got.metadata.resourceVersion == "0"
    assert got.kind == KIND


@pytest.mark.unit
def test_get_not_found() -> None:
    reg = SQLiteRegistry()
    with pytest.raises(NotFoundError):
        _run(reg.get(KIND, "team-a", "demo", TENANT_A))


# ---------------------------------------------------------------------- #
# 乐观并发 CAS
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_cas_update_success() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_make_resource()))
    assert created.metadata.resourceVersion == "0"

    updated = _run(reg.put(created, expected_version="0"))
    assert updated.metadata.resourceVersion == "1"

    got = _run(reg.get(KIND, "team-a", "demo", TENANT_A))
    assert got.metadata.resourceVersion == "1"


@pytest.mark.unit
def test_cas_update_conflict() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_make_resource()))

    with pytest.raises(ConflictError):
        _run(reg.put(created, expected_version="999"))


@pytest.mark.unit
def test_cas_update_on_missing_resource_is_conflict() -> None:
    reg = SQLiteRegistry()
    res = _make_resource()
    # 资源不存在时 CAS update 视为冲突
    with pytest.raises(ConflictError):
        _run(reg.put(res, expected_version="0"))


@pytest.mark.unit
def test_create_conflict_on_existing() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource()))

    with pytest.raises(ConflictError):
        _run(reg.put(_make_resource()))  # expected_version=None → Create


# ---------------------------------------------------------------------- #
# Delete
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_delete() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource()))

    _run(reg.delete(KIND, "team-a", "demo", TENANT_A))

    with pytest.raises(NotFoundError):
        _run(reg.get(KIND, "team-a", "demo", TENANT_A))


@pytest.mark.unit
def test_delete_not_found() -> None:
    reg = SQLiteRegistry()
    with pytest.raises(NotFoundError):
        _run(reg.delete(KIND, "team-a", "demo", TENANT_A))


# ---------------------------------------------------------------------- #
# 租户隔离
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_tenant_isolation() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource(tenant_id=TENANT_A)))

    # 租户 B 无法 get 到租户 A 的资源
    with pytest.raises(NotFoundError):
        _run(reg.get(KIND, "team-a", "demo", TENANT_B))

    # 租户 B 的 list 也看不到
    results = _run(reg.list(KIND, TENANT_B))
    assert results == []


# ---------------------------------------------------------------------- #
# List / 分页 / 过滤
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_with_pagination() -> None:
    reg = SQLiteRegistry()
    for i in range(5):
        _run(reg.put(_make_resource(name=f"res-{i}", namespace="ns")))

    page1 = _run(reg.list_page(KIND, TENANT_A, namespace="ns", limit=2))
    page2 = _run(
        reg.list_page(KIND, TENANT_A, namespace="ns", page_token=page1.next_page_token, limit=2)
    )
    page3 = _run(
        reg.list_page(KIND, TENANT_A, namespace="ns", page_token=page2.next_page_token, limit=2)
    )

    assert [len(page.items) for page in (page1, page2, page3)] == [2, 2, 1]
    assert page3.next_page_token is None
    all_names = [r.metadata.name for page in (page1, page2, page3) for r in page.items]
    assert all_names == ["res-0", "res-1", "res-2", "res-3", "res-4"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("count", "limit", "has_more"),
    [(0, 1, False), (2, 2, False), (2, 3, False), (3, 2, True)],
)
def test_list_page_returns_a_token_only_when_more_items_match(
    count: int, limit: int, has_more: bool
) -> None:
    reg = SQLiteRegistry()
    for i in range(count):
        _run(reg.put(_make_resource(name=f"res-{i}")))

    page = _run(reg.list_page(KIND, TENANT_A, limit=limit))

    assert len(page.items) == min(count, limit)
    assert (page.next_page_token is not None) is has_more


@pytest.mark.unit
def test_list_continues_from_a_list_page_token() -> None:
    reg = SQLiteRegistry()
    for i in range(3):
        _run(reg.put(_make_resource(name=f"res-{i}")))
    first = _run(reg.list_page(KIND, TENANT_A, limit=1))

    rest = _run(reg.list(KIND, TENANT_A, continue_token=first.next_page_token))

    assert [r.metadata.name for r in rest] == ["res-1", "res-2"]


@pytest.mark.unit
def test_list_rejects_an_invalid_continue_token() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource()))

    with pytest.raises(InvalidPageTokenError):
        _run(reg.list(KIND, TENANT_A, continue_token="not-a-token"))


@pytest.mark.unit
def test_page_token_does_not_bind_the_page_size() -> None:
    reg = SQLiteRegistry()
    for i in range(4):
        _run(reg.put(_make_resource(name=f"res-{i}")))
    first = _run(reg.list_page(KIND, TENANT_A, limit=1))

    second = _run(reg.list_page(KIND, TENANT_A, page_token=first.next_page_token, limit=10))

    assert [r.metadata.name for r in second.items] == ["res-1", "res-2", "res-3"]
    assert second.next_page_token is None


@pytest.mark.unit
def test_list_page_orders_cluster_scoped_resources_first() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource(name="b", namespace="ns")))
    _run(reg.put(_make_resource(name="z", namespace=None)))
    _run(reg.put(_make_resource(name="a", namespace="ns")))

    seen: list[tuple[str | None, str]] = []
    token = None
    while True:
        page = _run(reg.list_page(KIND, TENANT_A, page_token=token, limit=1))
        seen.extend((r.metadata.namespace, r.metadata.name) for r in page.items)
        token = page.next_page_token
        if token is None:
            break

    assert seen == [(None, "z"), ("ns", "a"), ("ns", "b")]


_BOUND_QUERY = {"kind": KIND, "tenant_id": TENANT_A, "namespace": "ns", "labels": {"env": "prod"}}


@pytest.mark.unit
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"tenant_id": TENANT_B}, id="other-tenant"),
        pytest.param({"kind": "Node"}, id="other-kind"),
        pytest.param({"namespace": "other"}, id="other-namespace"),
        pytest.param({"namespace": None}, id="all-namespaces"),
        pytest.param({"labels": None}, id="no-label-selector"),
        pytest.param({"labels": {"env": "dev"}}, id="other-label-value"),
        pytest.param({"labels": {"env": "prod", "tier": "web"}}, id="extra-label"),
    ],
)
def test_page_token_is_bound_to_tenant_kind_and_filters(change: dict) -> None:
    reg = SQLiteRegistry()
    for tenant_id in (TENANT_A, TENANT_B):
        for kind in (KIND, "Node"):
            for i in range(2):
                resource = _make_resource(
                    name=f"res-{i}", namespace="ns", tenant_id=tenant_id, kind=kind
                )
                resource.metadata.labels = {"env": "prod"}
                _run(reg.put(resource))
    token = _run(reg.list_page(**_BOUND_QUERY, limit=1)).next_page_token
    assert token is not None

    with pytest.raises(InvalidPageTokenError):
        _run(reg.list_page(**{**_BOUND_QUERY, **change}, page_token=token, limit=1))


@pytest.mark.unit
def test_page_token_ignores_the_order_of_label_selectors() -> None:
    reg = SQLiteRegistry()
    for i in range(2):
        resource = _make_resource(name=f"res-{i}")
        resource.metadata.labels = {"app": "web", "env": "prod"}
        _run(reg.put(resource))
    first = _run(reg.list_page(KIND, TENANT_A, labels={"app": "web", "env": "prod"}, limit=1))

    second = _run(
        reg.list_page(
            KIND,
            TENANT_A,
            labels={"env": "prod", "app": "web"},
            page_token=first.next_page_token,
            limit=1,
        )
    )

    assert [r.metadata.name for r in second.items] == ["res-1"]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _swap_payload(token: str) -> str:
    """把载荷换成跳过一个资源的合法载荷，保留原签名。"""
    payload = _b64(b'{"v":1,"ns":"","name":"res-1"}')
    return f"{payload}.{token.partition('.')[2]}"


def _alter_signature(token: str) -> str:
    return token[:-1] + ("B" if token.endswith("A") else "A")


@pytest.mark.unit
@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_swap_payload, id="swapped-payload"),
        pytest.param(_alter_signature, id="altered-signature"),
        pytest.param(lambda token: token.partition(".")[0], id="missing-signature"),
        pytest.param(lambda token: f"{token}.extra", id="trailing-data"),
        pytest.param(lambda token: f"{token}é", id="non-ascii"),
        pytest.param(lambda token: "", id="empty"),
        pytest.param(lambda token: "not-a-token", id="garbage"),
        pytest.param(
            lambda token: base64.urlsafe_b64encode(b'{"ns": "", "name": "res-0"}').decode(),
            id="legacy-unsigned-token",
        ),
    ],
)
def test_tampered_page_tokens_are_rejected(tamper: Callable[[str], str]) -> None:
    reg = SQLiteRegistry()
    for i in range(3):
        _run(reg.put(_make_resource(name=f"res-{i}")))
    token = _run(reg.list_page(KIND, TENANT_A, limit=1)).next_page_token

    with pytest.raises(InvalidPageTokenError):
        _run(reg.list_page(KIND, TENANT_A, page_token=tamper(token), limit=1))


@pytest.mark.unit
def test_page_tokens_survive_a_restart(tmp_path) -> None:
    db_file = str(tmp_path / "registry.db")
    reg = SQLiteRegistry(db_file)
    for i in range(3):
        _run(reg.put(_make_resource(name=f"res-{i}")))
    token = _run(reg.list_page(KIND, TENANT_A, limit=1)).next_page_token
    _run(reg.close())

    reopened = SQLiteRegistry(db_file)
    try:
        page = _run(reopened.list_page(KIND, TENANT_A, page_token=token, limit=5))
    finally:
        _run(reopened.close())

    assert [r.metadata.name for r in page.items] == ["res-1", "res-2"]


@pytest.mark.unit
def test_page_tokens_from_another_database_are_rejected() -> None:
    issuer, other = SQLiteRegistry(), SQLiteRegistry()
    for reg in (issuer, other):
        for i in range(2):
            _run(reg.put(_make_resource(name=f"res-{i}")))
    token = _run(issuer.list_page(KIND, TENANT_A, limit=1)).next_page_token

    with pytest.raises(InvalidPageTokenError):
        _run(other.list_page(KIND, TENANT_A, page_token=token, limit=1))


@pytest.mark.unit
def test_list_filter_by_namespace() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource(name="a", namespace="ns1")))
    _run(reg.put(_make_resource(name="b", namespace="ns2")))

    result = _run(reg.list(KIND, TENANT_A, namespace="ns1"))
    assert len(result) == 1
    assert result[0].metadata.name == "a"


@pytest.mark.unit
def test_list_filter_by_labels() -> None:
    reg = SQLiteRegistry()
    r1 = _make_resource(name="a", namespace="ns")
    r1.metadata.labels = {"env": "prod"}
    r2 = _make_resource(name="b", namespace="ns")
    r2.metadata.labels = {"env": "dev"}
    _run(reg.put(r1))
    _run(reg.put(r2))

    result = _run(reg.list(KIND, TENANT_A, namespace="ns", labels={"env": "prod"}))
    assert len(result) == 1
    assert result[0].metadata.name == "a"


@pytest.mark.unit
def test_list_watch_not_implemented() -> None:
    reg = SQLiteRegistry()
    with pytest.raises(NotImplementedError):
        _run(reg.list(KIND, TENANT_A, watch=True))


# ---------------------------------------------------------------------- #
# generation / resource_version
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_generation_bump() -> None:
    res = _make_resource(generation=3)
    bumped = bump_generation(res)
    assert bumped.metadata.generation == 4
    # 原对象不变
    assert res.metadata.generation == 3


@pytest.mark.unit
def test_resource_version_increments() -> None:
    reg = SQLiteRegistry()
    v0 = _run(reg.put(_make_resource()))
    assert v0.metadata.resourceVersion == "0"

    v1 = _run(reg.put(v0, expected_version="0"))
    assert v1.metadata.resourceVersion == "1"

    v2 = _run(reg.put(v1, expected_version="1"))
    assert v2.metadata.resourceVersion == "2"


# ---------------------------------------------------------------------- #
# Schema 校验
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_validate_resource_accepts_valid() -> None:
    validate_resource(_make_resource())  # 不抛异常


@pytest.mark.unit
def test_validate_resource_rejects_empty_name() -> None:
    res = _make_resource()
    res.metadata.name = ""
    with pytest.raises(InvalidStateTransition):
        validate_resource(res)


@pytest.mark.unit
def test_put_rejects_missing_tenant_id() -> None:
    reg = SQLiteRegistry()
    res = _make_resource()
    res.metadata.tenant_id = None
    with pytest.raises(ConflictError):
        _run(reg.put(res))


# ---------------------------------------------------------------------- #
# Allocation 持久化（development-plan 7.2 DDL：部分唯一索引兜底并发正确性）
# ---------------------------------------------------------------------- #


def _make_allocation(
    resource_id: str = "gpu-0",
    tenant_id: str = TENANT_A,
    idempotency_key: str = "idem-1",
) -> Allocation:
    """构造一个最小 Allocation（默认 Active）。"""
    return Allocation(
        resource_id=resource_id,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    )


@pytest.mark.unit
def test_allocation_unique_active_per_resource() -> None:
    """同一 resource_id 的第二个 Active 插入被部分唯一索引拒绝（ConflictError）。"""
    reg = SQLiteRegistry()
    _run(reg.put_allocation(_make_allocation(idempotency_key="idem-1")))

    with pytest.raises(ConflictError):
        _run(reg.put_allocation(_make_allocation(idempotency_key="idem-2")))


@pytest.mark.unit
def test_allocation_released_allows_new_active() -> None:
    """release 后（Active → Released）同一 resource_id 可再插入新 Active。"""
    reg = SQLiteRegistry()
    alloc_id = _run(reg.put_allocation(_make_allocation(idempotency_key="idem-1")))
    _run(reg.release_allocation(alloc_id, TENANT_A, reason="test"))

    # 释放后可再次为同一资源分配
    new_id = _run(reg.put_allocation(_make_allocation(idempotency_key="idem-2")))
    assert new_id != alloc_id
    active = _run(reg.get_active_allocation("gpu-0", TENANT_A))
    assert active is not None
    assert active["id"] == new_id
    assert active["status"] == "Active"


@pytest.mark.unit
def test_allocation_idempotency_key_conflict() -> None:
    """重复 idempotency_key 由唯一约束拒绝（转 ConflictError）。"""
    reg = SQLiteRegistry()
    _run(reg.put_allocation(_make_allocation(resource_id="gpu-0", idempotency_key="dup")))
    with pytest.raises(ConflictError):
        _run(reg.put_allocation(_make_allocation(resource_id="gpu-1", idempotency_key="dup")))


@pytest.mark.unit
def test_get_active_allocation_tenant_isolation() -> None:
    """跨租户读取 Active 分配返回 None（多租户边界）。"""
    reg = SQLiteRegistry()
    _run(reg.put_allocation(_make_allocation(tenant_id=TENANT_A)))

    assert _run(reg.get_active_allocation("gpu-0", TENANT_B)) is None
    assert _run(reg.get_active_allocation("gpu-0", TENANT_A)) is not None


@pytest.mark.unit
def test_release_allocation_rejects_non_active() -> None:
    """非 Active 分配 release 走状态机校验，抛 InvalidStateTransition。"""
    reg = SQLiteRegistry()
    alloc_id = _run(reg.put_allocation(_make_allocation()))
    _run(reg.release_allocation(alloc_id, TENANT_A))

    with pytest.raises(InvalidStateTransition):
        _run(reg.release_allocation(alloc_id, TENANT_A))


@pytest.mark.unit
def test_release_allocation_not_found() -> None:
    """不存在的分配 release 抛 NotFoundError。"""
    reg = SQLiteRegistry()
    with pytest.raises(NotFoundError):
        _run(reg.release_allocation("no-such-id", TENANT_A))


@pytest.mark.unit
def test_list_allocations_tenant_scoped() -> None:
    """list_allocations 仅返回本租户记录，且含终态历史。"""
    reg = SQLiteRegistry()
    a1 = _run(reg.put_allocation(_make_allocation(resource_id="gpu-0", idempotency_key="k1")))
    _run(reg.put_allocation(_make_allocation(resource_id="gpu-1", idempotency_key="k2")))
    _run(reg.release_allocation(a1, TENANT_A))
    _run(reg.put_allocation(_make_allocation(tenant_id=TENANT_B, idempotency_key="k3")))

    rows = _run(reg.list_allocations(TENANT_A))
    assert len(rows) == 2
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["Active", "Released"]


# ---------------------------------------------------------------------- #
# request_delete：finalizer 受控删除语义
# ---------------------------------------------------------------------- #


def _make_inference_service(
    name: str = "demo",
    namespace: str = "team-a",
    tenant_id: str = TENANT_A,
    phase: InferencePhase = InferencePhase.READY,
    generation: int = 1,
    finalizers: list[str] | None = None,
) -> InferenceService:
    """构造带强类型 spec/status 的 InferenceService（phase 由 status 携带）。"""
    return InferenceService(
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            tenant_id=tenant_id,
            generation=generation,
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=phase),
        finalizers=finalizers or [],
    )


def _stored_phase(reg: SQLiteRegistry, name: str = "demo") -> str | None:
    """直接读 DB 列取 phase（绕过 payload 反序列化，子类 body 仅 SQL 层校验）。"""
    row = reg._conn.execute(
        "SELECT phase, finalizers FROM resources WHERE name=?", (name,)
    ).fetchone()
    assert row is not None
    return row["phase"]


@pytest.mark.unit
def test_request_delete_side_effect_free_phase_physical() -> None:
    """无副作用态（Pending）DELETE → 直接物理删除（向后兼容）。"""
    reg = SQLiteRegistry()
    _run(reg.put(_make_inference_service(phase=InferencePhase.PENDING)))

    result = _run(reg.request_delete(KIND, "team-a", "demo", TENANT_A))

    assert result.deleted is True
    assert result.finalizers == []
    with pytest.raises(NotFoundError):
        _run(reg.get(KIND, "team-a", "demo", TENANT_A))


@pytest.mark.unit
def test_request_delete_legacy_payload_without_phase_physical() -> None:
    """基类资源（无 phase payload）DELETE → 直接物理删除（旧路径兼容）。"""
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource()))

    result = _run(reg.request_delete(KIND, "team-a", "demo", TENANT_A))

    assert result.deleted is True
    with pytest.raises(NotFoundError):
        _run(reg.get(KIND, "team-a", "demo", TENANT_A))


@pytest.mark.unit
def test_request_delete_ready_adds_default_finalizer() -> None:
    """有副作用态（Ready）DELETE → 自动追加默认 finalizer 并置 Terminating。"""
    reg = SQLiteRegistry()
    _run(reg.put(_make_inference_service(phase=InferencePhase.READY)))

    result = _run(reg.request_delete(KIND, "team-a", "demo", TENANT_A))

    assert result.deleted is False
    assert result.finalizers == [DEFAULT_RELEASE_FINALIZER]
    assert _stored_phase(reg) == "Terminating"
    # 资源仍存在（等待协调器清空 finalizer）
    assert _run(reg.get(KIND, "team-a", "demo", TENANT_A)) is not None


@pytest.mark.unit
def test_request_delete_existing_finalizers_kept() -> None:
    """已有 finalizers（任意 phase，含 Failed）→ 保留并置 Terminating。"""
    reg = SQLiteRegistry()
    custom = "example.com/cleanup"
    _run(reg.put(_make_inference_service(phase=InferencePhase.FAILED, finalizers=[custom])))

    result = _run(reg.request_delete(KIND, "team-a", "demo", TENANT_A))

    assert result.deleted is False
    assert result.finalizers == [custom]
    assert _stored_phase(reg) == "Terminating"


@pytest.mark.unit
def test_request_delete_idempotent_when_terminating() -> None:
    """已 Terminating 的重复删除请求幂等返回（不重复追加/递增）。"""
    reg = SQLiteRegistry()
    _run(reg.put(_make_inference_service(phase=InferencePhase.READY)))
    first = _run(reg.request_delete(KIND, "team-a", "demo", TENANT_A))
    assert first.deleted is False

    second = _run(reg.request_delete(KIND, "team-a", "demo", TENANT_A))

    assert second.deleted is False
    assert second.finalizers == first.finalizers


@pytest.mark.unit
def test_request_delete_not_found() -> None:
    """删除不存在的资源抛 NotFoundError。"""
    reg = SQLiteRegistry()
    with pytest.raises(NotFoundError):
        _run(reg.request_delete(KIND, "team-a", "demo", TENANT_A))


# ---------------------------------------------------------------------- #
# put() 状态机接线
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_put_rejects_illegal_phase_transition() -> None:
    """Pending → Ready 为非法跳变，put 拒绝（InvalidStateTransition 透传）。"""
    reg = SQLiteRegistry()
    _run(reg.put(_make_inference_service(phase=InferencePhase.PENDING)))

    jump = _make_inference_service(phase=InferencePhase.READY)
    with pytest.raises(InvalidStateTransition):
        _run(reg.put(jump, expected_version="0"))


@pytest.mark.unit
def test_put_allows_legal_phase_transition() -> None:
    """Pending → Scheduling 合法通过，版本递增。"""
    reg = SQLiteRegistry()
    _run(reg.put(_make_inference_service(phase=InferencePhase.PENDING)))

    step = _make_inference_service(phase=InferencePhase.SCHEDULING)
    updated = _run(reg.put(step, expected_version="0"))

    assert updated.metadata.resourceVersion == "1"
    assert _stored_phase(reg) == "Scheduling"


@pytest.mark.unit
def test_put_unmanaged_kind_skips_transition_check() -> None:
    """不受管 kind（非 InferenceService）不校验转移。"""
    reg = SQLiteRegistry()
    _run(reg.put(_make_resource(kind="Node", uid="u1")))
    _run(
        reg.put(
            _make_resource(kind="Node", uid="u1"),
            expected_version="0",
        )
    )  # 同状态重写，不抛


# ---------------------------------------------------------------------- #
# 存量库迁移（finalizers / phase 列幂等补齐）
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_legacy_db_migration_adds_columns(tmp_path) -> None:
    """旧结构库文件打开时幂等补 phase/finalizers 列，旧 payload 视为 []。"""
    db_file = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_file)
    legacy.executescript(
        """
        CREATE TABLE resources (
            tenant_id TEXT NOT NULL, kind TEXT NOT NULL, namespace TEXT, name TEXT NOT NULL,
            uid TEXT, resource_version INTEGER NOT NULL DEFAULT 0,
            generation INTEGER NOT NULL DEFAULT 0,
            observed_generation INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, kind, namespace, name));
        """
    )
    # 旧结构库中的 body 为合法基类 Resource JSON（无 finalizers 字段）
    legacy_res = Resource(
        kind=KIND,
        metadata=ObjectMeta(name="old", namespace="team-a", tenant_id=TENANT_A),
    )
    legacy.execute(
        "INSERT INTO resources (tenant_id, kind, namespace, name, body) VALUES (?, ?, ?, ?, ?)",
        (TENANT_A, KIND, "team-a", "old", legacy_res.model_dump_json(by_alias=True)),
    )
    legacy.commit()
    legacy.close()

    reg = SQLiteRegistry(str(db_file))
    columns = {row["name"] for row in reg._conn.execute("PRAGMA table_info(resources)")}
    assert {"phase", "finalizers"} <= columns
    # 旧 payload 无 finalizers 字段 → 读回视为 []
    got = _run(reg.get(KIND, "team-a", "old", TENANT_A))
    assert got.finalizers == []
    # 重复打开幂等
    _run(reg.close())
    reg2 = SQLiteRegistry(str(db_file))
    _run(reg2.close())
