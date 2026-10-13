"""资源注册表的审计记录：API 写入在资源写入的同一事务中记录调用主体。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
import yaml
from forgemesh.controlplane.audit import AuditEvent
from forgemesh.controlplane.auth import Principal
from forgemesh.controlplane.deployment import InMemoryLauncher, check_instances
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.objects import ObjectMeta
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

KIND = "InferenceService"
TENANT = "tenant-a"
NAMESPACE = "team-a"
ALICE = Principal(subject="alice", tenant_id=TENANT, scopes=frozenset({"write"}), key_id="key-a")
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


def _audit(reg: SQLiteRegistry) -> list[AuditEvent]:
    """按写入顺序返回租户的全部审计记录。"""
    return sorted(reg.audit_log.query_by_tenant(TENANT, limit=1000), key=lambda e: e.timestamp)


#: 每种 API 写入（带调用主体）：在已有 ``svc``（Pending）与 ``ready``（Ready）的注册表上执行。
_WRITES: dict[str, Callable[[SQLiteRegistry, Principal], Coroutine[Any, Any, Any]]] = {
    "create": lambda reg, who: reg.put(_service("new"), correlation_id="corr", principal=who),
    "update": lambda reg, who: reg.put(
        _service(), expected_version="0", correlation_id="corr", principal=who
    ),
    "spec-update": lambda reg, who: reg.update_spec(
        KIND,
        NAMESPACE,
        "svc",
        TENANT,
        NEW_SPEC,
        labels={},
        annotations={},
        correlation_id="corr",
        principal=who,
    ),
    "request-delete": lambda reg, who: reg.request_delete(
        KIND, NAMESPACE, "svc", TENANT, correlation_id="corr", principal=who
    ),
    "terminate": lambda reg, who: reg.request_delete(
        KIND, NAMESPACE, "ready", TENANT, correlation_id="corr", principal=who
    ),
    "delete": lambda reg, who: reg.delete(
        KIND, NAMESPACE, "svc", TENANT, correlation_id="corr", principal=who
    ),
}

#: 写入 → (审计 action, 资源名, data.change)。
_EXPECTED = {
    "create": ("create", "new", "created"),
    "update": ("update", "svc", "updated"),
    "spec-update": ("update", "svc", "updated"),
    "request-delete": ("delete", "svc", "deleted"),
    "terminate": ("delete", "ready", "terminating"),
    "delete": ("delete", "svc", "deleted"),
}


def _registry() -> SQLiteRegistry:
    reg = SQLiteRegistry()
    _run(reg.put(_service()))
    _run(reg.put(_service("ready", phase=InferencePhase.READY)))
    return reg


@pytest.mark.unit
@pytest.mark.parametrize("write", sorted(_WRITES))
def test_api_writes_record_the_principal_with_the_event(write: str) -> None:
    reg = _registry()
    _run(_WRITES[write](reg, ALICE))

    [record] = _audit(reg)
    *_, event = reg.outbox.get_pending()
    action, name, change = _EXPECTED[write]
    assert (record.tenant_id, record.actor, record.key_id) == (TENANT, "alice", "key-a")
    assert record.correlation_id == event.payload["correlationId"] == "corr"
    assert (record.action, record.outcome) == (action, "success")
    assert (record.resource_type, record.namespace, record.resource_id) == (KIND, NAMESPACE, name)
    assert record.details == {
        "change": change,
        "resourceUid": f"uid-{name}",
        "resourceVersion": event.payload["resourceVersion"],
        "eventId": event.event_id,
    }
    assert event.payload["data"]["change"] == change
    assert record.timestamp == event.created_at


@pytest.mark.unit
def test_the_recorded_actions_are_the_openapi_enum() -> None:
    """带调用主体的各种写入记下的 action 恰好是 OpenAPI ``AuditEvent.action`` 的枚举值。"""
    openapi = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "api" / "openapi" / "openapi.yaml").read_text(
            encoding="utf-8"
        )
    )
    actions = set()
    for write in _WRITES.values():
        reg = _registry()
        _run(write(reg, ALICE))
        actions.update(record.action for record in _audit(reg))

    assert actions == set(
        openapi["components"]["schemas"]["AuditEvent"]["properties"]["action"]["enum"]
    )


@pytest.mark.unit
def test_api_writes_without_a_correlation_id_share_a_generated_one() -> None:
    reg = SQLiteRegistry()
    _run(reg.put(_service(), principal=ALICE))

    [record] = _audit(reg)
    [event] = reg.outbox.get_pending()
    assert record.correlation_id
    assert record.correlation_id == event.payload["correlationId"]


@pytest.mark.unit
def test_internal_writes_are_not_audited() -> None:
    """控制器的 status 写入（含实例核对）与协调器的写入（写回 finalizer、物理删除）不给出
    调用主体：照常写入事件，不写审计记录。"""
    reg = _registry()
    gone = Endpoint(url="http://10.0.0.1:8000", instanceId="gone", protocol="openai-chat")
    patch = {"endpoints": [gone.model_dump(mode="json")]}
    _run(reg.update_status(KIND, NAMESPACE, "ready", TENANT, patch))
    assert [key.name for key in _run(check_instances(reg, InMemoryLauncher()))] == ["ready"]
    svc = _run(reg.update_status(KIND, NAMESPACE, "svc", TENANT, {"phase": "Scheduling"}))
    svc.finalizers = ["example.com/cleanup"]
    _run(reg.put(svc, expected_version=svc.metadata.resourceVersion, actor="reconciler"))
    _run(reg.delete(KIND, NAMESPACE, "svc", TENANT, actor="reconciler"))

    assert _audit(reg) == []
    assert len(reg.outbox.get_pending()) == 7


@pytest.mark.unit
@pytest.mark.parametrize("write", sorted(_WRITES))
def test_a_principal_of_another_tenant_cannot_write(write: str) -> None:
    reg = _registry()
    before = reg.outbox.get_stats()
    mallory = Principal(subject="mallory", tenant_id="tenant-b", key_id="key-b")

    with pytest.raises(ValueError, match="tenant-b"):
        _run(_WRITES[write](reg, mallory))

    assert _audit(reg) == []
    assert reg.outbox.get_stats() == before
    assert reg.audit_log.query_by_tenant("tenant-b") == []


def _state(reg: SQLiteRegistry) -> tuple[list[tuple[Any, ...]], ...]:
    """资源、变更记录、事件与审计记录四张表的全部内容。"""
    return tuple(
        [tuple(row) for row in reg._conn.execute(sql)]
        for sql in (
            "SELECT * FROM resources ORDER BY name",
            "SELECT * FROM resource_changes ORDER BY seq",
            "SELECT * FROM outbox_events ORDER BY event_id",
            "SELECT * FROM audit_events ORDER BY audit_id",
        )
    )


@pytest.mark.unit
def test_an_unchanged_spec_update_writes_no_audit_record() -> None:
    reg = _registry()
    current = _run(reg.get(KIND, NAMESPACE, "svc", TENANT))
    spec = current.model_dump(mode="json", by_alias=True)["spec"]
    before = _state(reg)

    _run(
        reg.update_spec(
            KIND,
            NAMESPACE,
            "svc",
            TENANT,
            spec,
            labels={},
            annotations={},
            correlation_id="corr",
            principal=ALICE,
        )
    )

    assert _state(reg) == before


@pytest.mark.unit
@pytest.mark.parametrize("write", sorted(_WRITES))
def test_a_write_failing_after_the_audit_record_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    reg = _registry()
    before = _state(reg)
    original = reg._emit

    def emit_then_fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        assert reg._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(reg, "_emit", emit_then_fail)
    with pytest.raises(RuntimeError, match="disk I/O error"):
        _run(_WRITES[write](reg, ALICE))
    monkeypatch.undo()

    assert _state(reg) == before
    _run(_WRITES[write](reg, ALICE))
    assert len(_audit(reg)) == 1


@pytest.mark.unit
@pytest.mark.parametrize("write", sorted(_WRITES))
def test_an_audit_write_failure_rolls_back_the_resource_and_the_event(
    monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    reg = _registry()
    before = _state(reg)

    def unavailable(event: AuditEvent) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(reg.audit_log, "put", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        _run(_WRITES[write](reg, ALICE))
    monkeypatch.undo()

    assert _state(reg) == before
