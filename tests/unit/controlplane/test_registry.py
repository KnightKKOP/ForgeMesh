"""``forgemesh.controlplane.registry`` 单元测试。

覆盖 SQLiteRegistry 契约实现的核心语义：

* Create / Get 往返；
* 乐观并发 CAS（成功 / 冲突）；
* 重复 Create 抛 ConflictError；
* Delete（成功 / NotFound）；
* 租户隔离（跨租户 Get 抛 NotFoundError）；
* 分页游标（5 条分 3 页取完）；
* namespace 过滤；
* generation 自增；
* resource_version 递增；
* labels 过滤、watch 未实现、Schema 校验。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""
from __future__ import annotations

import asyncio

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.controlplane.registry.models import bump_generation, validate_resource
from forgemesh.core.errors import ConflictError, InvalidStateTransition, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource

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

    page1 = _run(reg.list(KIND, TENANT_A, namespace="ns", limit=2))
    assert len(page1) == 2

    token = SQLiteRegistry._encode_token(
        page1[-1].metadata.namespace, page1[-1].metadata.name
    )
    page2 = _run(
        reg.list(KIND, TENANT_A, namespace="ns", limit=2, continue_token=token)
    )
    assert len(page2) == 2

    token = SQLiteRegistry._encode_token(
        page2[-1].metadata.namespace, page2[-1].metadata.name
    )
    page3 = _run(
        reg.list(KIND, TENANT_A, namespace="ns", limit=2, continue_token=token)
    )
    assert len(page3) == 1

    all_names = [r.metadata.name for r in page1 + page2 + page3]
    assert all_names == ["res-0", "res-1", "res-2", "res-3", "res-4"]


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
