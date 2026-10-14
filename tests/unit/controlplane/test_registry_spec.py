"""注册中心期望状态写入（``update_spec``）单元测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from forgemesh.controlplane.registry import ChangeType, ConflictReason, SQLiteRegistry
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
from pydantic import ValidationError

KIND = "InferenceService"
TENANT = "tenant-a"
NAMESPACE = "team-a"
SPEC: dict[str, Any] = {
    "model": {"reference": "models/llama"},
    "runtime": {"provider": "vllm"},
    "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}},
}
NEW_SPEC: dict[str, Any] = {**SPEC, "model": {"reference": "models/mistral"}}


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _service(
    *,
    generation: int = 1,
    labels: dict[str, str] | None = None,
    phase: InferencePhase = InferencePhase.PENDING,
) -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(
            name="svc",
            namespace=NAMESPACE,
            tenant_id=TENANT,
            uid="uid-1",
            generation=generation,
            labels=labels or {},
            annotations={"team": "search"},
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=phase),
    )


def _update(reg: SQLiteRegistry, spec: dict[str, Any] = NEW_SPEC, **kwargs: Any) -> Resource:
    kwargs.setdefault("labels", {})
    kwargs.setdefault("annotations", {"team": "search"})
    return _run(reg.update_spec(KIND, NAMESPACE, "svc", TENANT, spec, **kwargs))


def _stored_row(reg: SQLiteRegistry):
    return reg._conn.execute(
        "SELECT body, generation, observed_generation, resource_version FROM resources "
        "WHERE name='svc'"
    ).fetchone()


def _changes(reg: SQLiteRegistry) -> list[ChangeType]:
    return [change.change for change in _run(reg.changes_since(0))]


@pytest.mark.unit
def test_spec_change_bumps_generation_and_version() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    _run(
        reg.update_status(
            KIND,
            NAMESPACE,
            "svc",
            TENANT,
            {
                "phase": "Scheduling",
                "conditions": [{"type": "Ready", "status": "False", "reason": "Scheduling"}],
            },
            observed_generation=1,
        )
    )
    before = _run(reg.get(KIND, NAMESPACE, "svc", TENANT))

    updated = _update(reg, expected_version="1", expected_uid="uid-1")

    assert isinstance(updated, InferenceService)
    assert updated.spec.model.reference == "models/mistral"
    assert (updated.metadata.generation, updated.metadata.observed_generation) == (2, 1)
    assert updated.metadata.resourceVersion == "2"
    assert updated.metadata.uid == "uid-1"
    assert updated.status == before.status
    assert updated.finalizers == before.finalizers
    assert _run(reg.get(KIND, NAMESPACE, "svc", TENANT)) == updated
    row = _stored_row(reg)
    assert (row["generation"], row["observed_generation"], row["resource_version"]) == (2, 1, 2)
    assert _changes(reg) == [ChangeType.CREATED, ChangeType.STATUS, ChangeType.UPDATED]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("labels", "annotations"),
    [
        pytest.param({"tier": "gold"}, {"team": "search"}, id="labels"),
        pytest.param({}, {"team": "ads"}, id="annotations"),
    ],
)
def test_metadata_changes_keep_the_generation(
    labels: dict[str, str], annotations: dict[str, str]
) -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    updated = _update(reg, SPEC, labels=labels, annotations=annotations)

    assert (updated.metadata.labels, updated.metadata.annotations) == (labels, annotations)
    assert updated.metadata.generation == 1
    assert updated.metadata.resourceVersion == "1"
    assert _changes(reg) == [ChangeType.CREATED, ChangeType.UPDATED]


@pytest.mark.unit
def test_labels_and_annotations_are_replaced_as_a_whole() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service(labels={"tier": "gold", "zone": "a"})))

    updated = _update(reg, SPEC, labels={"tier": "gold"}, annotations={})

    assert updated.metadata.labels == {"tier": "gold"}
    assert updated.metadata.annotations == {}
    body = json.loads(_stored_row(reg)["body"])
    assert (body["metadata"]["labels"], body["metadata"]["annotations"]) == ({"tier": "gold"}, {})


@pytest.mark.unit
def test_unchanged_update_is_not_written() -> None:
    """spec 规范化后比较：省略的字段取默认值，与已存储的完整 spec 相同时不写库。"""
    reg = SQLiteRegistry()
    created = _run(reg.put(_service()))

    unchanged = _update(reg, SPEC, labels={}, expected_version="0")

    assert unchanged == created
    assert _stored_row(reg)["resource_version"] == 0
    assert _changes(reg) == [ChangeType.CREATED]


@pytest.mark.unit
def test_stale_resource_version_is_a_conflict() -> None:
    """两个写入方读到同一版本：先写的成功，后写的冲突，不会覆盖前者。"""
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    _update(reg, expected_version="0")

    with pytest.raises(ConflictError) as excinfo:
        _update(reg, {**SPEC, "model": {"reference": "models/qwen"}}, expected_version="0")

    assert excinfo.value.details == {"reason": ConflictReason.RESOURCE_VERSION}
    stored = _run(reg.get(KIND, NAMESPACE, "svc", TENANT))
    assert stored.spec.model.reference == "models/mistral"
    assert stored.metadata.resourceVersion == "1"


@pytest.mark.unit
def test_uid_mismatch_is_a_conflict() -> None:
    """同名资源被删除后重建：版本从 0 重新计数，uid 不同仍然冲突。"""
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    with pytest.raises(ConflictError) as excinfo:
        _update(reg, expected_version="0", expected_uid="uid-of-a-deleted-service")

    assert excinfo.value.details == {"reason": ConflictReason.UID}
    assert _stored_row(reg)["resource_version"] == 0


@pytest.mark.unit
def test_terminating_resource_rejects_updates() -> None:
    """Terminating 先于版本检查：即使版本过期也报告 Terminating（重试无意义）。"""
    reg = SQLiteRegistry()
    _run(reg.put(_service(phase=InferencePhase.READY)))
    assert not _run(reg.request_delete(KIND, NAMESPACE, "svc", TENANT)).deleted

    for expected_version in (None, "0"):
        with pytest.raises(ConflictError) as excinfo:
            _update(reg, expected_version=expected_version)
        assert excinfo.value.details == {"reason": ConflictReason.TERMINATING}
    assert _stored_row(reg)["resource_version"] == 1


@pytest.mark.unit
def test_legacy_rows_with_generation_zero_count_up_from_zero() -> None:
    """1.1.0 之前创建的资源 generation 为 0：不迁移，第一次 spec 变化后为 1。"""
    reg = SQLiteRegistry()
    _run(reg.put(_service(generation=0)))

    updated = _update(reg)

    assert (updated.metadata.generation, updated.metadata.observed_generation) == (1, 0)


@pytest.mark.unit
@pytest.mark.parametrize(
    "spec",
    [
        pytest.param({**SPEC, "replicas": 3}, id="unknown-field"),
        pytest.param({key: SPEC[key] for key in ("model", "runtime")}, id="missing-resources"),
    ],
)
def test_invalid_spec_is_rejected(spec: dict[str, Any]) -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    with pytest.raises(ValidationError):
        _update(reg, spec)

    assert _stored_row(reg)["resource_version"] == 0


@pytest.mark.unit
def test_invalid_labels_are_rejected() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    with pytest.raises(ValidationError):
        _update(reg, labels={"tier": 1})


@pytest.mark.unit
def test_missing_resource_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        _update(SQLiteRegistry())


@pytest.mark.unit
def test_kinds_without_a_model_are_not_supported() -> None:
    reg = SQLiteRegistry()
    widget = Resource(kind="Widget", metadata=ObjectMeta(name="w", tenant_id=TENANT))
    _run(reg.put(widget))

    with pytest.raises(ValueError, match="not supported"):
        _run(reg.update_spec("Widget", None, "w", TENANT, {}, labels={}, annotations={}))
