"""租户隔离安全测试（IVT-7）。

验证不同租户无法读取彼此私有资源与审计数据。覆盖以下隔离边界：

* **配额隔离**：租户 A 超配额不影响租户 B 的配额决策。
* **注册表隔离**：租户 A 的资源对租户 B 不可见（list / get / delete 均隔离）。
* **审计隔离**：租户 A 的审计记录对租户 B 不可见。
* **决策记录隔离**：租户 A 的决策记录对租户 B 不可见。
* **诊断包隔离**：诊断包只返回请求租户的数据，绝不泄露其他租户信息。
* **跨租户访问**：跨租户 get / delete 一律返回 NotFoundError，不泄露存在性。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from forgemesh.controlplane.audit import AuditEvent, AuditLog
from forgemesh.controlplane.diagnostics import DiagnosticCollector
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.decision.provider import DecisionCandidate
from forgemesh.decision.records import DecisionRecord, DecisionRecordStore
from forgemesh.policy.quota import QuotaPolicy

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
KIND = "InferenceService"
RESOURCE_TYPE = "InferenceService"


def _run(coro: Any) -> Any:
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_resource(
    name: str = "demo",
    tenant_id: str = TENANT_A,
    namespace: str = "team-a",
    uid: str = "uid-1",
) -> Resource:
    """构造一个可复用的最小 Resource。"""
    return Resource(
        kind=KIND,
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            uid=uid,
            tenant_id=tenant_id,
        ),
    )


def _make_audit_event(
    tenant_id: str,
    resource_id: str = "res-1",
    action: str = "create",
    correlation_id: str = "cid-1",
) -> AuditEvent:
    """构造一个可复用的审计事件。"""
    return AuditEvent(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        actor="actor-1",
        action=action,
        resource_type=KIND,
        resource_id=resource_id,
        outcome="success",
    )


def _make_decision_record(
    record_id: str,
    tenant_id: str,
    request_id: str = "req-1",
) -> DecisionRecord:
    """构造一个可复用的决策记录。"""
    return DecisionRecord(
        record_id=record_id,
        request_id=request_id,
        tenant_id=tenant_id,
        provider_id="provider-1",
        provider_version="1.0.0",
        operation="allocate",
        candidates=[
            DecisionCandidate(candidate_id="c1", action="assign"),
        ],
        chosen=DecisionCandidate(candidate_id="c1", action="assign"),
        success=True,
        risk_level="low",
    )


# --------------------------------------------------------------------------- #
# 配额隔离
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_quota_tenant_isolation() -> None:
    """租户 A 超配额不影响租户 B：配额以 ``{tenant_id}:{resource_type}`` 为键隔离。"""
    policy = QuotaPolicy()
    _run(policy.set_limit(TENANT_A, RESOURCE_TYPE, max=2))
    _run(policy.set_limit(TENANT_B, RESOURCE_TYPE, max=2))

    # 租户 A 消费至配额上限
    d1 = _run(policy.consume(TENANT_A, RESOURCE_TYPE, amount=2))
    assert d1.allowed is True

    # 租户 A 再消费 → 超配额拒绝
    d2 = _run(policy.consume(TENANT_A, RESOURCE_TYPE, amount=1))
    assert d2.allowed is False
    assert d2.reason == "quota_exceeded"

    # 租户 B 仍可正常消费（不受租户 A 超配额影响）
    d3 = _run(policy.consume(TENANT_B, RESOURCE_TYPE, amount=1))
    assert d3.allowed is True
    assert d3.reason == "quota_ok"

    # 租户 B 用量独立于租户 A
    usage_b = _run(policy.get_usage(TENANT_B, RESOURCE_TYPE))
    assert usage_b is not None
    assert usage_b.current == 1
    usage_a = _run(policy.get_usage(TENANT_A, RESOURCE_TYPE))
    assert usage_a is not None
    assert usage_a.current == 2


# --------------------------------------------------------------------------- #
# 注册表隔离
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_registry_tenant_isolation() -> None:
    """租户 A 的资源对租户 B 不可见：list 只返回该租户的资源。"""
    reg = SQLiteRegistry()
    res_a = _make_resource(name="svc-a", tenant_id=TENANT_A, uid="uid-a")
    res_b = _make_resource(name="svc-b", tenant_id=TENANT_B, uid="uid-b")

    _run(reg.put(res_a))
    _run(reg.put(res_b))

    # 租户 A 只能看到自己的资源
    list_a = _run(reg.list(KIND, TENANT_A))
    assert len(list_a) == 1
    assert list_a[0].metadata.name == "svc-a"

    # 租户 B 只能看到自己的资源
    list_b = _run(reg.list(KIND, TENANT_B))
    assert len(list_b) == 1
    assert list_b[0].metadata.name == "svc-b"

    # 租户 B 看不到租户 A 的资源（list 结果中不含 svc-a）
    names_b = {r.metadata.name for r in list_b}
    assert "svc-a" not in names_b


# --------------------------------------------------------------------------- #
# 审计隔离
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_audit_tenant_isolation() -> None:
    """租户 A 的审计记录对租户 B 不可见：query_by_tenant 只返回该租户的记录。"""
    audit = AuditLog()
    event_a = _make_audit_event(TENANT_A, resource_id="res-a", correlation_id="cid-a")
    event_b = _make_audit_event(TENANT_B, resource_id="res-b", correlation_id="cid-b")

    audit.record(event_a)
    audit.record(event_b)

    # 租户 A 只能查到自己的审计记录
    records_a = audit.query_by_tenant(TENANT_A)
    assert len(records_a) == 1
    assert records_a[0].tenant_id == TENANT_A
    assert records_a[0].resource_id == "res-a"

    # 租户 B 只能查到自己的审计记录
    records_b = audit.query_by_tenant(TENANT_B)
    assert len(records_b) == 1
    assert records_b[0].tenant_id == TENANT_B
    assert records_b[0].resource_id == "res-b"

    # 租户 B 查询结果中不含租户 A 的 resource_id
    resource_ids_b = {r.resource_id for r in records_b}
    assert "res-a" not in resource_ids_b


# --------------------------------------------------------------------------- #
# 决策记录隔离
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_decision_tenant_isolation() -> None:
    """租户 A 的决策记录对租户 B 不可见：list_by_tenant 只返回该租户的记录。"""
    store = DecisionRecordStore()
    record_a = _make_decision_record("rec-a", TENANT_A, request_id="req-a")
    record_b = _make_decision_record("rec-b", TENANT_B, request_id="req-b")

    _run(store.save(record_a))
    _run(store.save(record_b))

    # 租户 A 只能查到自己的决策记录
    records_a = _run(store.list_by_tenant(TENANT_A))
    assert len(records_a) == 1
    assert records_a[0].tenant_id == TENANT_A
    assert records_a[0].record_id == "rec-a"

    # 租户 B 只能查到自己的决策记录
    records_b = _run(store.list_by_tenant(TENANT_B))
    assert len(records_b) == 1
    assert records_b[0].tenant_id == TENANT_B
    assert records_b[0].record_id == "rec-b"

    # 租户 B 查询结果中不含租户 A 的 record_id
    record_ids_b = {r.record_id for r in records_b}
    assert "rec-a" not in record_ids_b


# --------------------------------------------------------------------------- #
# 诊断包隔离
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_diagnostics_tenant_isolation() -> None:
    """诊断包只返回请求租户的数据，绝不泄露其他租户信息。"""
    collector = DiagnosticCollector()

    # 注册租户 A 与租户 B 的资源、工作流、决策、日志
    collector.register_resource(TENANT_A, {"id": "res-a", "type": "gpu"})
    collector.register_resource(TENANT_B, {"id": "res-b", "type": "gpu"})

    collector.register_workflow(TENANT_A, {"id": "wf-a", "status": "running"})
    collector.register_workflow(TENANT_B, {"id": "wf-b", "status": "running"})

    collector.register_decision(TENANT_A, {"id": "dec-a", "outcome": "success"})
    collector.register_decision(TENANT_B, {"id": "dec-b", "outcome": "success"})

    now = time.time()
    collector.register_log(
        TENANT_A,
        {"timestamp": int(now), "level": "info", "message": "tenant-a log"},
    )
    collector.register_log(
        TENANT_B,
        {"timestamp": int(now), "level": "info", "message": "tenant-b log"},
    )

    # 插件为全局共享，不按租户隔离
    collector.register_plugin({"name": "plugin-1", "version": "1.0.0"})

    # 采集租户 A 的诊断包
    bundle_a = _run(collector.collect(TENANT_A))

    assert bundle_a.tenant_id == TENANT_A
    # 资源只含租户 A 的
    assert len(bundle_a.resources) == 1
    assert bundle_a.resources[0]["id"] == "res-a"
    # 工作流只含租户 A 的
    assert len(bundle_a.workflows) == 1
    assert bundle_a.workflows[0]["id"] == "wf-a"
    # 决策只含租户 A 的
    assert len(bundle_a.decisions) == 1
    assert bundle_a.decisions[0]["id"] == "dec-a"
    # 日志只含租户 A 的
    assert len(bundle_a.logs) == 1
    assert "tenant-a" in bundle_a.logs[0]["message"]
    # 插件全局共享（两个租户都能看到）
    assert len(bundle_a.plugins) == 1

    # 采集租户 B 的诊断包，确认不含租户 A 的数据
    bundle_b = _run(collector.collect(TENANT_B))

    assert bundle_b.tenant_id == TENANT_B
    assert bundle_b.resources[0]["id"] == "res-b"
    assert bundle_b.workflows[0]["id"] == "wf-b"
    assert bundle_b.decisions[0]["id"] == "dec-b"
    assert "tenant-b" in bundle_b.logs[0]["message"]

    # 租户 B 的诊断包中绝不出现租户 A 的标识
    all_b_data = str(bundle_b.resources) + str(bundle_b.workflows) + str(bundle_b.decisions)
    assert "res-a" not in all_b_data
    assert "wf-a" not in all_b_data
    assert "dec-a" not in all_b_data


# --------------------------------------------------------------------------- #
# 跨租户访问返回 NotFound
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_cross_tenant_access_returns_not_found() -> None:
    """跨租户访问返回 NotFoundError，不泄露资源存在性。"""
    reg = SQLiteRegistry()
    res_a = _make_resource(name="svc-a", tenant_id=TENANT_A, uid="uid-a")
    _run(reg.put(res_a))

    # 租户 B 试图 get 租户 A 的资源 → NotFoundError（而非泄露存在性的 403）
    with pytest.raises(NotFoundError):
        _run(reg.get(KIND, "team-a", "svc-a", TENANT_B))

    # 租户 B 试图 delete 租户 A 的资源 → NotFoundError
    with pytest.raises(NotFoundError):
        _run(reg.delete(KIND, "team-a", "svc-a", TENANT_B))

    # 租户 B list 看不到租户 A 的资源
    list_b = _run(reg.list(KIND, TENANT_B))
    assert len(list_b) == 0

    # 跨租户访问的错误消息不应包含其他租户的敏感信息（仅含请求租户自身标识）
    try:
        _run(reg.get(KIND, "team-a", "svc-a", TENANT_B))
    except NotFoundError as exc:
        # 错误消息中包含请求方租户 B 的标识，但不暴露租户 A 的资源内容
        assert TENANT_B in exc.message or "not found" in exc.message.lower()
