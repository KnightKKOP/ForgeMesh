"""资源注册表的资源变更事件：与资源写入在同一事务中写入 Outbox（ADR-008）。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from forgemesh.controlplane.deployment import (
    INSTANCE_LOST_REASON,
    InMemoryLauncher,
    check_instances,
    reclaim_orphaned_allocations,
)
from forgemesh.controlplane.events import DomainEvent, EventProducer, event_type_prefix
from forgemesh.controlplane.outbox import Dispatcher, OutboxEvent
from forgemesh.controlplane.registry import DEFAULT_RELEASE_FINALIZER, ChangeType, SQLiteRegistry
from forgemesh.controlplane.version import CONTROL_PLANE_VERSION
from forgemesh.core.errors import ConflictError, InvalidStateTransition, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import (
    AcceleratorSpec,
    Endpoint,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)
from jsonschema import Draft202012Validator
from pydantic import ValidationError

EVENT_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "api" / "schemas" / "event.schema.json").read_text(
        encoding="utf-8"
    )
)

KIND = "InferenceService"
TENANT = "tenant-a"
NAMESPACE = "team-a"
#: ``_service`` 的 spec 换一个模型（``update_spec`` 的一次期望状态变更）。
NEW_SPEC: dict[str, Any] = {
    "model": {"reference": "models/mistral"},
    "runtime": {"provider": "vllm"},
    "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}},
}


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
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


def _serving(name: str = "svc") -> InferenceService:
    """``Ready`` 的服务，它的 endpoint 指向启动器不认识（即已不在运行）的实例。"""
    service = _service(name, phase=InferencePhase.READY)
    service.status.endpoints = [
        Endpoint(url="http://10.0.0.1:8000", instanceId=f"gone-{name}", protocol="openai-chat")
    ]
    return service


def _events(reg: SQLiteRegistry) -> list[OutboxEvent]:
    """按写入顺序返回全部 pending 事件；每个信封都须符合 ``event.schema.json``。"""
    events = reg.outbox.get_pending()
    for event in events:
        Draft202012Validator(EVENT_SCHEMA).validate(event.payload)
    return events


def _dump(resource: Resource) -> dict[str, Any]:
    return resource.model_dump(mode="json", by_alias=True)


@pytest.mark.unit
def test_create_writes_a_created_event_in_the_envelope_schema() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_service(), correlation_id="corr-create"))

    [event] = _events(reg)
    payload = event.payload
    assert event.event_id == payload["eventId"]
    assert (event.aggregate_type, event.aggregate_id, event.event_type) == (
        KIND,
        f"{TENANT}/{NAMESPACE}/svc",
        "inference.service.created",
    )
    assert payload["eventType"] == "inference.service.created"
    assert payload["schemaVersion"] == "v1"
    assert (payload["tenantId"], payload["resourceId"], payload["resourceUid"]) == (
        TENANT,
        "svc",
        "uid-svc",
    )
    assert payload["resourceVersion"] == 0
    assert payload["correlationId"] == "corr-create"
    assert payload["producer"] == {"component": "control-plane", "version": CONTROL_PLANE_VERSION}
    assert payload["data"] == {
        "kind": KIND,
        "namespace": NAMESPACE,
        "name": "svc",
        "change": "created",
        "phase": "Pending",
        "resource": _dump(created),
    }
    assert payload["occurredAt"].endswith("Z")
    occurred = datetime.fromisoformat(payload["occurredAt"])
    assert occurred.utcoffset() == timedelta(0)
    assert occurred.timestamp() == pytest.approx(event.created_at)


@pytest.mark.unit
def test_writes_without_a_correlation_id_get_a_generated_one() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service("a")))
    _run(reg.put(_service("b")))

    first, second = (event.payload["correlationId"] for event in _events(reg))
    assert first and second and first != second


@pytest.mark.unit
def test_update_and_status_writes_carry_the_state_after_the_write() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_service()))
    created.metadata.labels["tier"] = "gold"
    updated = _run(reg.put(created, expected_version="0", correlation_id="corr-update"))
    scheduling = _run(
        reg.update_status(
            KIND, NAMESPACE, "svc", TENANT, {"phase": "Scheduling"}, correlation_id="corr-status"
        )
    )

    _, update_event, status_event = _events(reg)
    assert update_event.event_type == "inference.service.updated"
    assert update_event.payload["resourceVersion"] == 1
    assert update_event.payload["correlationId"] == "corr-update"
    assert update_event.payload["data"]["change"] == "updated"
    assert update_event.payload["data"]["resource"] == _dump(updated)
    assert status_event.event_type == "inference.service.status_updated"
    assert status_event.payload["resourceVersion"] == 2
    assert status_event.payload["correlationId"] == "corr-status"
    assert status_event.payload["data"]["change"] == "status_updated"
    assert status_event.payload["data"]["phase"] == "Scheduling"
    assert status_event.payload["data"]["resource"] == _dump(scheduling)


@pytest.mark.unit
def test_spec_updates_emit_an_updated_event_with_the_state_after_the_write() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    updated = _run(
        reg.update_spec(
            KIND,
            NAMESPACE,
            "svc",
            TENANT,
            NEW_SPEC,
            labels={"tier": "gold"},
            annotations={},
            expected_version="0",
            correlation_id="corr-spec",
        )
    )

    _, event = _events(reg)
    assert event.event_type == "inference.service.updated"
    assert event.payload["resourceVersion"] == 1
    assert event.payload["correlationId"] == "corr-spec"
    assert event.payload["data"]["change"] == "updated"
    assert event.payload["data"]["phase"] == "Pending"
    assert event.payload["data"]["resource"] == _dump(updated)
    assert event.payload["data"]["resource"]["spec"]["model"]["reference"] == "models/mistral"


@pytest.mark.unit
def test_unchanged_spec_update_emits_no_event() -> None:
    reg = SQLiteRegistry()
    created = _run(reg.put(_service()))
    spec = _dump(created)["spec"]
    _run(reg.update_spec(KIND, NAMESPACE, "svc", TENANT, spec, labels={}, annotations={}))

    assert [event.event_type for event in _events(reg)] == ["inference.service.created"]


@pytest.mark.unit
def test_unchanged_status_write_emits_no_event() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    patch = {"phase": "Scheduling"}
    _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, patch))
    _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, patch))

    assert [event.event_type for event in _events(reg)] == [
        "inference.service.created",
        "inference.service.status_updated",
    ]


@pytest.mark.unit
def test_the_instance_check_reports_a_lost_instance_with_a_status_event() -> None:
    """实例核对把实例已不在运行的 ``Ready`` 服务转为 ``Degraded``：这次 status 写入同样写入
    事件；核对不属于哪一次协调，关联 ID 是生成的。"""
    reg = SQLiteRegistry()
    _run(reg.put(_serving(), correlation_id="corr-create"))

    lost = _run(check_instances(reg, InMemoryLauncher(), after_restart=True))

    degraded = _run(reg.get(KIND, NAMESPACE, "svc", TENANT))
    _, event = _events(reg)
    assert [key.name for key in lost] == ["svc"]
    assert event.event_type == "inference.service.status_updated"
    assert event.payload["resourceVersion"] == 1
    assert event.payload["correlationId"] not in ("", "corr-create")
    assert event.payload["data"]["phase"] == "Degraded"
    assert event.payload["data"]["resource"] == _dump(degraded)
    [ready] = event.payload["data"]["resource"]["status"]["conditions"]
    assert (ready["status"], ready["reason"]) == ("False", INSTANCE_LOST_REASON)
    assert _run(check_instances(reg, InMemoryLauncher())) == []
    assert len(_events(reg)) == 2


@pytest.mark.unit
def test_delete_requests_emit_deleted_or_terminating_events() -> None:
    reg = SQLiteRegistry()
    pending = _run(reg.put(_service("pending")))
    _run(reg.put(_service("ready", phase=InferencePhase.READY)))

    _run(reg.request_delete(KIND, NAMESPACE, "pending", TENANT, correlation_id="corr-delete"))
    _run(reg.request_delete(KIND, NAMESPACE, "ready", TENANT, correlation_id="corr-terminate"))
    _run(reg.request_delete(KIND, NAMESPACE, "ready", TENANT))  # 幂等：不再写入

    deleted, terminating = _events(reg)[2:]
    assert deleted.event_type == "inference.service.deleted"
    assert deleted.payload["correlationId"] == "corr-delete"
    assert deleted.payload["resourceVersion"] == 0
    assert deleted.payload["data"]["phase"] == "Pending"
    assert deleted.payload["data"]["resource"] == _dump(pending)
    assert terminating.event_type == "inference.service.terminating"
    assert terminating.payload["correlationId"] == "corr-terminate"
    assert terminating.payload["resourceVersion"] == 1
    assert terminating.payload["data"]["phase"] == "Terminating"
    resource = terminating.payload["data"]["resource"]
    assert resource["finalizers"] == [DEFAULT_RELEASE_FINALIZER]
    assert resource["status"]["phase"] == "Terminating"
    assert resource["metadata"]["resourceVersion"] == "1"


@pytest.mark.unit
def test_delete_emits_the_last_state_of_the_resource() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    scheduling = _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, {"phase": "Scheduling"}))

    _run(reg.delete(KIND, NAMESPACE, "svc", TENANT, actor="reconciler", correlation_id="corr-gc"))
    with pytest.raises(NotFoundError):
        _run(reg.delete(KIND, NAMESPACE, "svc", TENANT))

    *_, deleted = _events(reg)
    assert len(_events(reg)) == 3
    assert deleted.event_type == "inference.service.deleted"
    assert deleted.payload["correlationId"] == "corr-gc"
    assert deleted.payload["resourceVersion"] == 1
    assert deleted.payload["data"]["phase"] == "Scheduling"
    assert deleted.payload["data"]["resource"] == _dump(scheduling)


@pytest.mark.unit
def test_base_resources_get_events_without_a_status_subtree() -> None:
    reg = SQLiteRegistry()
    widget = Resource(
        kind="Widget",
        metadata=ObjectMeta(name="w", tenant_id=TENANT),
        finalizers=["example.com/cleanup"],
    )
    _run(reg.put(widget))
    _run(reg.request_delete("Widget", None, "w", TENANT))

    created, terminating = _events(reg)
    assert created.event_type == "widget.created"
    assert created.aggregate_id == f"{TENANT}//w"
    assert "resourceUid" not in created.payload
    assert created.payload["data"]["namespace"] is None
    assert created.payload["data"]["phase"] is None
    assert terminating.event_type == "widget.terminating"
    assert terminating.payload["data"]["phase"] == "Terminating"
    assert "status" not in terminating.payload["data"]["resource"]


@pytest.mark.unit
def test_rejected_writes_leave_no_event() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))

    with pytest.raises(ConflictError):
        _run(reg.put(_service()))
    with pytest.raises(ConflictError):
        _run(reg.put(_service(), expected_version="7"))
    with pytest.raises(ConflictError):
        _run(
            reg.update_status(
                KIND, NAMESPACE, "svc", TENANT, {"phase": "Scheduling"}, expected_version="7"
            )
        )
    with pytest.raises(InvalidStateTransition):
        _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, {"phase": "Ready"}))
    with pytest.raises(ConflictError):
        _run(
            reg.update_spec(
                KIND,
                NAMESPACE,
                "svc",
                TENANT,
                NEW_SPEC,
                labels={},
                annotations={},
                expected_uid="x",
            )
        )
    with pytest.raises(NotFoundError):
        _run(reg.request_delete(KIND, NAMESPACE, "missing", TENANT))

    assert [event.event_type for event in _events(reg)] == ["inference.service.created"]


class _Releaser:
    """记录被停止部署的 uid 的 :class:`~forgemesh.controlplane.deployment.DeploymentReleaser`。"""

    def __init__(self) -> None:
        self.released: list[str] = []

    async def release_uid(self, uid: str) -> None:
        self.released.append(uid)


@pytest.mark.unit
def test_allocation_writes_and_the_orphan_reclaim_emit_no_event() -> None:
    """分配不是资源：建立、释放分配与回收孤立的分配都不写事件。"""
    reg = SQLiteRegistry()
    for index, name in enumerate(("svc", "gone")):
        meta = _run(reg.put(_service(name))).metadata
        owner = ResourceRef(kind=KIND, namespace=NAMESPACE, name=name, uid=meta.uid)
        device = AllocatedDevice(node_id="node-a", device_id=f"gpu-{index}")
        _run(reg.create_allocation(TENANT, owner, f"{meta.uid}:1", [device]))
    assert _run(reg.release_active_allocation("uid-svc", TENANT)) is not None
    _run(reg.delete(KIND, NAMESPACE, "gone", TENANT))
    releaser = _Releaser()

    [reclaimed] = _run(reclaim_orphaned_allocations(reg, releaser))

    assert releaser.released == ["uid-gone"]
    assert _run(reg.get_allocation(reclaimed))["status"] == "Expired"
    assert [event.event_type for event in _events(reg)] == [
        "inference.service.created",
        "inference.service.created",
        "inference.service.deleted",
    ]


#: 每种资源写入：在已有 ``svc``（Pending）与 ``ready``（Ready，实例已不在运行）的注册表上
#: 执行；``instance-check`` 是实例核对把 ``ready`` 转为 ``Degraded`` 的 status 写入。
_WRITES: dict[str, Callable[[SQLiteRegistry], Coroutine[Any, Any, Any]]] = {
    "create": lambda reg: reg.put(_service("new")),
    "update": lambda reg: reg.put(_service(), expected_version="0"),
    "spec-update": lambda reg: reg.update_spec(
        KIND, NAMESPACE, "svc", TENANT, NEW_SPEC, labels={}, annotations={}
    ),
    "status": lambda reg: reg.update_status(
        KIND, NAMESPACE, "svc", TENANT, {"phase": "Scheduling"}
    ),
    "request-delete": lambda reg: reg.request_delete(KIND, NAMESPACE, "svc", TENANT),
    "terminate": lambda reg: reg.request_delete(KIND, NAMESPACE, "ready", TENANT),
    "delete": lambda reg: reg.delete(KIND, NAMESPACE, "svc", TENANT),
    "instance-check": lambda reg: check_instances(reg, InMemoryLauncher()),
}


def _state(reg: SQLiteRegistry) -> tuple[list[tuple[Any, ...]], ...]:
    """资源、变更记录与事件三张表的全部内容。"""
    return tuple(
        [tuple(row) for row in reg._conn.execute(sql)]
        for sql in (
            "SELECT * FROM resources ORDER BY name",
            "SELECT * FROM resource_changes ORDER BY seq",
            "SELECT * FROM outbox_events ORDER BY event_id",
        )
    )


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["_record_change", "_emit"])
@pytest.mark.parametrize("write", sorted(_WRITES))
def test_a_failing_write_leaves_neither_the_resource_nor_the_event(
    monkeypatch: pytest.MonkeyPatch, write: str, stage: str
) -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    _run(reg.put(_serving("ready")))
    before = _state(reg)
    original = getattr(reg, stage)

    def fail_after(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(reg, stage, fail_after)
    with pytest.raises(RuntimeError, match="disk I/O error"):
        _run(_WRITES[write](reg))
    monkeypatch.undo()

    assert _state(reg) == before
    _run(_WRITES[write](reg))
    assert len(_events(reg)) == 3


@pytest.mark.unit
def test_an_event_write_failure_rolls_back_the_resource_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = SQLiteRegistry()

    def unavailable(event: OutboxEvent) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(reg.outbox, "put", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        _run(reg.put(_service()))
    monkeypatch.undo()

    with pytest.raises(NotFoundError):
        _run(reg.get(KIND, NAMESPACE, "svc", TENANT))
    assert _run(reg.changes_since(0)) == []
    assert _events(reg) == []


@pytest.mark.unit
def test_events_are_persisted_with_the_resource(tmp_path: Path) -> None:
    db_path = str(tmp_path / "cp.db")
    reg = SQLiteRegistry(db_path)
    _run(reg.put(_service(), correlation_id="corr-create"))
    _run(reg.close())

    reopened = SQLiteRegistry(db_path)
    [event] = _events(reopened)
    assert event.payload["correlationId"] == "corr-create"
    assert [change.change for change in _run(reopened.changes_since(0))] == [ChangeType.CREATED]
    _run(reopened.close())


@pytest.mark.unit
def test_dispatcher_publishes_registry_events() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], dict[str, int]]:
        reg = SQLiteRegistry()
        await reg.put(_service())
        published: list[dict[str, Any]] = []
        await Dispatcher(reg.outbox, lambda event: published.append(event.payload)).dispatch_once()
        stats = reg.outbox.get_stats()
        await reg.close()
        return published, stats

    published, stats = _run(scenario())
    assert [payload["eventType"] for payload in published] == ["inference.service.created"]
    assert stats["published"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "prefix"),
    [
        ("InferenceService", "inference.service"),
        ("GPUNode", "gpu.node"),
        ("DeviceProfile", "device.profile"),
        ("Widget", "widget"),
        ("S3Bucket", "s3.bucket"),
        ("plugin", "plugin"),
        ("---", "resource"),
    ],
)
def test_event_type_prefix_splits_the_kind_into_dotted_words(kind: str, prefix: str) -> None:
    assert event_type_prefix(kind) == prefix


@pytest.mark.unit
def test_domain_event_payload_is_utc_and_omits_empty_optional_fields() -> None:
    event = DomainEvent(
        event_type="inference.service.ready",
        schema_version="v1",
        occurred_at=datetime(2026, 10, 9, 20, 0, tzinfo=timezone(timedelta(hours=8))),
        tenant_id="team-a",
        resource_id="llama-service",
        correlation_id="corr-456",
        producer=EventProducer(component="inference-controller", version="1.0.0"),
        data={"phase": "Ready"},
    )

    payload = event.to_payload()
    Draft202012Validator(EVENT_SCHEMA).validate(payload)
    assert payload["occurredAt"] == "2026-10-09T12:00:00Z"
    assert "resourceUid" not in payload
    assert "resourceVersion" not in payload
    with pytest.raises(ValidationError):
        DomainEvent.model_validate({**payload, "occurredAt": datetime(2026, 10, 9, 12, 0)})
    with pytest.raises(ValidationError):
        DomainEvent.model_validate({**payload, "unexpected": True})
    assert DomainEvent.model_validate(payload).occurred_at == datetime(2026, 10, 9, 12, tzinfo=UTC)
