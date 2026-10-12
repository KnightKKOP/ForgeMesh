"""注册中心 status 子资源（``update_status``）单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from forgemesh.controlplane.registry import ChangeType, SQLiteRegistry
from forgemesh.controlplane.registry.store import _merge_conditions
from forgemesh.core.errors import ConflictError, InvalidStateTransition, NotFoundError
from forgemesh.core.objects import Condition, ObjectMeta, Resource
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
NAMESPACE = "team-a"
NOT_SCHEDULED = {
    "type": "Ready",
    "status": "False",
    "reason": "SchedulerUnavailable",
    "message": "no scheduler",
}


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _service(generation: int = 0) -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(
            name="svc", namespace=NAMESPACE, tenant_id=TENANT, generation=generation
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
    )


def _stored_row(reg: SQLiteRegistry, name: str = "svc"):
    return reg._conn.execute(
        "SELECT body, phase, observed_generation, resource_version, generation "
        "FROM resources WHERE name=?",
        (name,),
    ).fetchone()


@pytest.mark.unit
def test_status_update_touches_only_status_and_observed_generation() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_service(generation=2)))

    updated = _run(
        reg.update_status(
            KIND,
            NAMESPACE,
            "svc",
            TENANT,
            {"conditions": [NOT_SCHEDULED]},
            observed_generation=2,
            expected_version="0",
        )
    )

    assert isinstance(updated, InferenceService)
    [condition] = updated.status.conditions
    assert (condition.type, condition.status, condition.reason) == (
        "Ready",
        "False",
        "SchedulerUnavailable",
    )
    assert condition.lastTransitionTime is not None
    assert updated.conditions == []
    assert updated.spec == created.spec
    assert updated.status.observedGeneration == 2
    assert updated.metadata.observed_generation == 2
    assert updated.metadata.generation == 2
    assert updated.metadata.resourceVersion == "1"
    assert _run(reg.get(KIND, NAMESPACE, "svc", TENANT)) == updated
    row = _stored_row(reg)
    assert (row["resource_version"], row["generation"], row["observed_generation"]) == (1, 2, 2)


@pytest.mark.unit
def test_unchanged_status_is_not_written_again() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    patch = {"conditions": [NOT_SCHEDULED]}

    first = _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, patch, observed_generation=0))
    second = _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, patch, observed_generation=0))

    assert first.metadata.resourceVersion == second.metadata.resourceVersion == "1"
    assert [change.change for change in _run(reg.changes_since(0))] == [
        ChangeType.CREATED,
        ChangeType.STATUS,
    ]


@pytest.mark.unit
def test_status_update_is_compare_and_swap() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    with pytest.raises(ConflictError):
        _run(
            reg.update_status(
                KIND,
                NAMESPACE,
                "svc",
                TENANT,
                {"conditions": [NOT_SCHEDULED]},
                expected_version="7",
            )
        )

    assert _stored_row(reg)["resource_version"] == 0


@pytest.mark.unit
def test_phase_changes_follow_the_state_machine() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    scheduling = _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, {"phase": "Scheduling"}))
    with pytest.raises(InvalidStateTransition):
        _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, {"phase": "Ready"}))

    assert scheduling.status.phase == "Scheduling"
    assert _stored_row(reg)["phase"] == "Scheduling"


@pytest.mark.unit
def test_base_resources_keep_conditions_at_the_top_level() -> None:
    reg = SQLiteRegistry()
    widget = Resource(
        kind="Widget", metadata=ObjectMeta(name="w", namespace=NAMESPACE, tenant_id=TENANT)
    )
    _run(reg.put(widget))

    updated = _run(
        reg.update_status("Widget", NAMESPACE, "w", TENANT, {"conditions": [NOT_SCHEDULED]})
    )

    assert [condition.reason for condition in updated.conditions] == ["SchedulerUnavailable"]
    with pytest.raises(ValueError):
        _run(reg.update_status("Widget", NAMESPACE, "w", TENANT, {"phase": "Ready"}))


@pytest.mark.unit
def test_payload_fields_unknown_to_the_registry_are_preserved() -> None:
    class Gadget(Resource):
        spec: dict[str, str]
        status: dict[str, Any]

    reg = SQLiteRegistry()
    gadget = Gadget(
        kind="Gadget",
        metadata=ObjectMeta(name="g", namespace=NAMESPACE, tenant_id=TENANT),
        spec={"size": "large"},
        status={"phase": "Pending"},
    )
    _run(reg.put(gadget))

    _run(reg.update_status("Gadget", NAMESPACE, "g", TENANT, {"conditions": [NOT_SCHEDULED]}))

    body = json.loads(_stored_row(reg, "g")["body"])
    assert body["spec"] == {"size": "large"}
    assert body["status"]["phase"] == "Pending"
    assert body["status"]["conditions"][0]["reason"] == "SchedulerUnavailable"


@pytest.mark.unit
@pytest.mark.parametrize(
    "patch",
    [
        {"spec": {"replicas": 3}},
        {"conditions": [{"type": "Ready", "status": "Maybe"}]},
    ],
)
def test_invalid_patches_are_rejected(patch) -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    with pytest.raises(ValueError):
        _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, patch))


@pytest.mark.unit
def test_status_update_of_missing_resource_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        _run(SQLiteRegistry().update_status(KIND, NAMESPACE, "nope", TENANT, {"conditions": []}))


@pytest.mark.unit
def test_transition_time_changes_only_when_condition_status_changes() -> None:
    day_one = datetime(2026, 1, 1, tzinfo=UTC)
    day_two = datetime(2026, 1, 2, tzinfo=UTC)

    first = _merge_conditions([], [Condition(type="Ready", status="False", reason="A")], day_one)
    same_status = _merge_conditions(
        first, [Condition(type="Ready", status="False", reason="B")], day_two
    )
    flipped = _merge_conditions(
        same_status, [Condition(type="Ready", status="True", reason="C")], day_two
    )
    added = _merge_conditions(flipped, [Condition(type="Scheduled", status="True")], day_two)

    assert first == [
        {
            "type": "Ready",
            "status": "False",
            "reason": "A",
            "message": "",
            "lastTransitionTime": "2026-01-01T00:00:00Z",
        }
    ]
    assert same_status[0]["reason"] == "B"
    assert same_status[0]["lastTransitionTime"] == "2026-01-01T00:00:00Z"
    assert flipped[0]["lastTransitionTime"] == "2026-01-02T00:00:00Z"
    assert [condition["type"] for condition in added] == ["Ready", "Scheduled"]
