"""注册中心变更记录单元测试：同事务追加、增量读取、清理，以及跨租户键列表。"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.controlplane.registry import ChangeType, ResourceKey, SQLiteRegistry
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
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
TENANT = "tenant-a"
NAMESPACE = "team-a"


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _resource(
    name: str,
    namespace: str | None = NAMESPACE,
    tenant_id: str = TENANT,
    kind: str = KIND,
) -> Resource:
    return Resource(
        kind=kind,
        metadata=ObjectMeta(name=name, namespace=namespace, tenant_id=tenant_id),
    )


def _ready_service(name: str) -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(name=name, namespace=NAMESPACE, tenant_id=TENANT),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=InferencePhase.READY),
    )


def _names_and_changes(reg: SQLiteRegistry, seq: int = 0) -> list[tuple[str, ChangeType]]:
    return [(change.key.name, change.change) for change in _run(reg.changes_since(seq))]


@pytest.mark.unit
def test_every_successful_write_appends_one_change() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_resource("a")))
    _run(reg.put(created, expected_version=created.metadata.resourceVersion))
    _run(reg.put(_resource("b")))
    _run(reg.request_delete(KIND, NAMESPACE, "b", TENANT))
    _run(reg.delete(KIND, NAMESPACE, "a", TENANT))

    changes = _run(reg.changes_since(0))

    assert [(change.key.name, change.change) for change in changes] == [
        ("a", ChangeType.CREATED),
        ("a", ChangeType.UPDATED),
        ("b", ChangeType.CREATED),
        ("b", ChangeType.DELETED),
        ("a", ChangeType.DELETED),
    ]
    assert changes[0].key == ResourceKey(TENANT, KIND, NAMESPACE, "a")
    assert [change.seq for change in changes] == sorted({change.seq for change in changes})


@pytest.mark.unit
def test_delete_request_that_starts_termination_is_recorded_once() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_ready_service("svc")))

    _run(reg.request_delete(KIND, NAMESPACE, "svc", TENANT))
    _run(reg.request_delete(KIND, NAMESPACE, "svc", TENANT))

    assert _names_and_changes(reg) == [
        ("svc", ChangeType.CREATED),
        ("svc", ChangeType.TERMINATING),
    ]


@pytest.mark.unit
def test_failed_writes_append_nothing() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_resource("a")))

    with pytest.raises(ConflictError):
        _run(reg.put(_resource("a")))
    with pytest.raises(ConflictError):
        _run(reg.put(created, expected_version="41"))
    with pytest.raises(NotFoundError):
        _run(reg.delete(KIND, NAMESPACE, "missing", TENANT))

    assert _names_and_changes(reg) == [("a", ChangeType.CREATED)]


@pytest.mark.unit
def test_changes_are_read_incrementally_and_pruned_without_reusing_seq() -> None:
    reg = SQLiteRegistry()
    for name in ("a", "b", "c"):
        _run(reg.put(_resource(name)))

    first_page = _run(reg.changes_since(0, limit=2))
    rest = _run(reg.changes_since(first_page[-1].seq))
    latest = _run(reg.latest_change_seq())

    assert [change.key.name for change in first_page] == ["a", "b"]
    assert [change.key.name for change in rest] == ["c"]
    assert latest == rest[-1].seq

    assert _run(reg.prune_changes(latest)) == 3
    assert _run(reg.changes_since(0)) == []
    assert _run(reg.latest_change_seq()) == 0

    _run(reg.put(_resource("d")))
    after_prune = _run(reg.changes_since(latest))
    assert [change.key.name for change in after_prune] == ["d"]
    assert after_prune[0].seq > latest


@pytest.mark.unit
def test_changes_since_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        _run(SQLiteRegistry().changes_since(0, limit=0))


@pytest.mark.unit
def test_list_resource_keys_spans_tenants_and_filters_kinds() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_resource("x", tenant_id="tenant-b")))
    _run(reg.put(_resource("y", namespace=None)))
    _run(reg.put(_resource("w", kind="Widget")))

    assert _run(reg.list_resource_keys([KIND])) == [
        ResourceKey(TENANT, KIND, None, "y"),
        ResourceKey("tenant-b", KIND, NAMESPACE, "x"),
    ]
    assert len(_run(reg.list_resource_keys())) == 3
    assert _run(reg.list_resource_keys([])) == []
