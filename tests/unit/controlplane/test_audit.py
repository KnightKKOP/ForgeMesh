"""``forgemesh.controlplane.audit`` 单元测试。

覆盖审计事件模型与 AuditLog 的核心语义：

* 记录与按 correlation_id 查询；
* 同一 correlation_id 多条事件按时间排序（调用链）；
* 按租户查询；
* limit 生效；
* 审计事件包含全字段（actor/action/outcome/details）；
* correlation_id 全链路贯穿：create→allocate→deploy 完整链可查；
* ``put`` 只在调用方的事务中写入，``record`` 自行提交；与业务存储共用数据库；
* 存量库补齐 ``key_id`` / ``namespace`` 列。
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from forgemesh.controlplane.audit import AuditEvent, AuditLog
from forgemesh.controlplane.sqlite import Database

TENANT_A = "tenant-a"
CORRELATION_ID = "corr-1234"


def _make_event(
    correlation_id: str = CORRELATION_ID,
    tenant_id: str = TENANT_A,
    actor: str = "user@example.com",
    action: str = "create",
    resource_type: str = "InferenceService",
    resource_id: str = "svc-1",
    outcome: str = "success",
    details: dict | None = None,
    timestamp: float | None = None,
) -> AuditEvent:
    """构造一个可复用的最小 AuditEvent。"""
    return AuditEvent(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        details=details or {"source": "api"},
        timestamp=timestamp if timestamp is not None else time.time(),
    )


# ---------------------------------------------------------------------- #
# 记录与查询
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_record_and_query_by_correlation() -> None:
    """记录后可按 correlation_id 查到。"""
    log = AuditLog()
    event = _make_event()
    audit_id = log.record(event)

    assert audit_id == event.audit_id

    results = log.query_by_correlation(CORRELATION_ID)
    assert len(results) == 1
    assert results[0].audit_id == event.audit_id
    assert results[0].action == "create"


@pytest.mark.unit
def test_query_by_correlation_chain() -> None:
    """同一 correlation_id 多条事件按时间排序。"""
    log = AuditLog()
    base = time.time()
    log.record(_make_event(action="create", timestamp=base + 2))
    log.record(_make_event(action="allocate", timestamp=base + 1))
    log.record(_make_event(action="deploy", timestamp=base + 3))

    results = log.query_by_correlation(CORRELATION_ID)
    assert len(results) == 3
    # 按时间升序
    assert results[0].action == "allocate"
    assert results[1].action == "create"
    assert results[2].action == "deploy"


@pytest.mark.unit
def test_query_by_tenant() -> None:
    """按租户查询。"""
    log = AuditLog()
    log.record(_make_event(action="create", tenant_id=TENANT_A))
    log.record(_make_event(action="delete", tenant_id="tenant-b"))

    results = log.query_by_tenant(TENANT_A)
    assert len(results) == 1
    assert results[0].action == "create"


@pytest.mark.unit
def test_query_by_tenant_limit() -> None:
    """limit 生效。"""
    log = AuditLog()
    base = time.time()
    for i in range(5):
        log.record(_make_event(action=f"action-{i}", timestamp=base + i))

    results = log.query_by_tenant(TENANT_A, limit=2)
    assert len(results) == 2
    # 按时间降序，最新的在前
    assert results[0].action == "action-4"
    assert results[1].action == "action-3"


# ---------------------------------------------------------------------- #
# 字段完整性
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_audit_event_fields() -> None:
    """审计事件包含全字段（actor/action/outcome/details）。"""
    log = AuditLog()
    event = _make_event(
        actor="admin@forgemesh.io",
        action="allocate",
        resource_type="Accelerator",
        resource_id="gpu-0",
        outcome="success",
        details={"device": "A100", "count": 2},
    )
    log.record(event)

    results = log.query_by_correlation(CORRELATION_ID)
    assert len(results) == 1
    got = results[0]
    assert got.actor == "admin@forgemesh.io"
    assert got.action == "allocate"
    assert got.resource_type == "Accelerator"
    assert got.resource_id == "gpu-0"
    assert got.outcome == "success"
    assert got.details == {"device": "A100", "count": 2}
    assert got.tenant_id == TENANT_A
    assert got.correlation_id == CORRELATION_ID


# ---------------------------------------------------------------------- #
# correlation_id 全链路贯穿
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_correlation_id_tracing() -> None:
    """模拟一个请求链：create→allocate→deploy，全部共享同一 correlation_id，可查到完整链。"""
    log = AuditLog()
    correlation_id = "req-chain-001"
    base = time.time()

    # 步骤 1：创建 InferenceService
    log.record(
        _make_event(
            correlation_id=correlation_id,
            actor="user@example.com",
            action="create",
            resource_type="InferenceService",
            resource_id="svc-prod-1",
            outcome="success",
            details={"replicas": 2},
            timestamp=base,
        )
    )
    # 步骤 2：分配加速器
    log.record(
        _make_event(
            correlation_id=correlation_id,
            actor="scheduler@forgemesh.io",
            action="allocate",
            resource_type="Accelerator",
            resource_id="gpu-pool-0",
            outcome="success",
            details={"allocated": 2, "device": "A100"},
            timestamp=base + 1,
        )
    )
    # 步骤 3：部署到节点
    log.record(
        _make_event(
            correlation_id=correlation_id,
            actor="deployer@forgemesh.io",
            action="deploy",
            resource_type="Node",
            resource_id="node-worker-3",
            outcome="success",
            details={"image": "vllm:v0.6", "port": 8080},
            timestamp=base + 2,
        )
    )

    # 通过 correlation_id 查到完整调用链
    chain = log.query_by_correlation(correlation_id)
    assert len(chain) == 3
    assert chain[0].action == "create"
    assert chain[1].action == "allocate"
    assert chain[2].action == "deploy"

    # 所有事件共享同一 correlation_id
    for evt in chain:
        assert evt.correlation_id == correlation_id

    # 验证各步骤的 actor 与 resource
    assert chain[0].actor == "user@example.com"
    assert chain[0].resource_type == "InferenceService"
    assert chain[1].actor == "scheduler@forgemesh.io"
    assert chain[1].resource_type == "Accelerator"
    assert chain[2].actor == "deployer@forgemesh.io"
    assert chain[2].resource_type == "Node"

    # 验证 details 往返
    assert chain[0].details == {"replicas": 2}
    assert chain[1].details == {"allocated": 2, "device": "A100"}
    assert chain[2].details == {"image": "vllm:v0.6", "port": 8080}


# ---------------------------------------------------------------------- #
# 事务与共用数据库
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_put_requires_the_callers_transaction() -> None:
    """put 不自行提交：没有进行中的事务时拒绝写入。"""
    log = AuditLog()
    with pytest.raises(RuntimeError, match="transaction"):
        log.put(_make_event())
    assert log.query_by_tenant(TENANT_A) == []


@pytest.mark.unit
def test_put_commits_or_rolls_back_with_the_caller() -> None:
    """put 的记录随调用方的事务一起提交或回滚。"""
    log = AuditLog()
    kept, dropped = _make_event(action="create"), _make_event(action="delete")
    with log.database.transaction():
        log.put(kept)
    with pytest.raises(RuntimeError, match="boom"), log.database.transaction():
        log.put(dropped)
        raise RuntimeError("boom")

    assert [e.audit_id for e in log.query_by_tenant(TENANT_A)] == [kept.audit_id]


@pytest.mark.unit
def test_principal_fields_round_trip() -> None:
    """凭证 id 与命名空间随记录保存。"""
    log = AuditLog()
    log.record(_make_event().model_copy(update={"key_id": "key-a", "namespace": "team-a"}))

    [got] = log.query_by_correlation(CORRELATION_ID)
    assert (got.key_id, got.namespace) == ("key-a", "team-a")


@pytest.mark.unit
def test_shared_database_is_closed_by_its_owner() -> None:
    """共用的数据库由创建者关闭：AuditLog.close 不关闭它。"""
    database = Database()
    log = AuditLog(database=database)
    log.record(_make_event())
    log.close()

    assert database.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 1
    database.close()


@pytest.mark.unit
def test_legacy_table_gets_the_new_columns(tmp_path) -> None:
    """v1.0.0 的 audit_events 没有 key_id / namespace：打开时补齐，旧记录保留。"""
    path = str(tmp_path / "audit.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE audit_events (
            audit_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            actor TEXT, action TEXT NOT NULL, resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL, outcome TEXT NOT NULL, details TEXT,
            timestamp REAL NOT NULL
        );
        INSERT INTO audit_events VALUES
            ('old', 'corr-old', 'tenant-a', 'bob', 'create', 'InferenceService', 'svc',
             'success', NULL, 1.0);
        """
    )
    conn.close()

    log = AuditLog(path)
    log.record(_make_event().model_copy(update={"key_id": "key-a", "namespace": "team-a"}))
    reopened = AuditLog(path)

    old, new = sorted(reopened.query_by_tenant(TENANT_A), key=lambda e: e.timestamp)
    assert (old.audit_id, old.actor, old.key_id, old.namespace) == ("old", "bob", None, None)
    assert (new.key_id, new.namespace) == ("key-a", "team-a")
    log.close()
    reopened.close()
