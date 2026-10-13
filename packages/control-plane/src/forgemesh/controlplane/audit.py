"""审计记录与关联 ID 追踪。

对齐 development-plan 5.5 / INV-4。审计记录关键操作，
correlation_id 全链路贯穿，支持调用链查询。

资源注册表在 API 写入资源的同一个事务中写入审计记录（:meth:`AuditLog.put`），记录调用
主体（租户、主体、凭证 id）与关联 ID：资源写入失败时不会留下审计记录。
:meth:`AuditLog.record` 单独写入并提交一条记录。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .sqlite import Database

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS audit_events (
    audit_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    actor TEXT,
    key_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    namespace TEXT,
    resource_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id, timestamp);
"""

#: v1.0.0 的 ``audit_events`` 没有的列（打开存量库时补齐）。
_ADDED_COLUMNS = (
    ("key_id", "key_id TEXT"),
    ("namespace", "namespace TEXT"),
)


class AuditEvent(BaseModel):
    """审计事件。

    Attributes:
        audit_id: 审计记录唯一标识（UUID）。
        correlation_id: 关联 ID，全链路贯穿，用于调用链查询。
        tenant_id: 租户标识。
        actor: 操作发起者（用户/服务账号），可为 None。
        key_id: 发起者使用的凭证标识（API key 的 ``id``），可为 None。
        action: 操作类型（如 create/allocate/deploy）。
        resource_type: 资源类型。
        namespace: 资源的命名空间，可为 None。
        resource_id: 资源标识。
        outcome: 操作结果（success/failure/denied）。
        details: 附加详情（JSON 可序列化字典），可为 None。
        timestamp: 事件时间戳。
    """

    model_config = ConfigDict(extra="forbid")

    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str
    tenant_id: str
    actor: str | None = None
    key_id: str | None = None
    action: str
    resource_type: str
    namespace: str | None = None
    resource_id: str
    outcome: str  # success / failure / denied
    details: dict[str, Any] | None = None
    timestamp: float = Field(default_factory=time.time)


class AuditLog:
    """审计日志（SQLite 后端）。

    Args:
        db_path: SQLite 数据库路径，默认 :memory:。
        database: 与业务存储共用的数据库；给出时忽略 ``db_path``，:meth:`close`
            不关闭它（由创建者关闭）。
    """

    def __init__(self, db_path: str = ":memory:", *, database: Database | None = None) -> None:
        self._owns_db = database is None
        self._db = database if database is not None else Database(db_path)
        self._conn = self._db.conn
        self._init_schema()

    def _init_schema(self) -> None:
        """创建表与索引（幂等），并为存量库补齐新增的列。"""
        with self._db.lock:
            self._conn.executescript(_SCHEMA_SQL)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(audit_events)")}
            for name, ddl in _ADDED_COLUMNS:
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE audit_events ADD COLUMN {ddl}")
                    logger.info("migrated audit_events: added column %r", name)
            self._conn.commit()

    @property
    def database(self) -> Database:
        """审计表所在的数据库，供业务写入与审计记录共用同一个连接和锁。"""
        return self._db

    def put(self, event: AuditEvent) -> None:
        """在调用方的事务中写入一条审计记录，不提交：记录随业务写入一起提交或回滚。

        调用方须持有 :attr:`database` 的锁并已开启事务（同 :meth:`Outbox.put
        <forgemesh.controlplane.outbox.Outbox.put>`）。

        Raises:
            RuntimeError: 连接上没有进行中的事务（记录不会写入）。
        """
        if not self._conn.in_transaction:
            raise RuntimeError("AuditLog.put must run inside the caller's transaction")
        self._conn.execute(
            "INSERT INTO audit_events (audit_id, correlation_id, tenant_id, actor, key_id, "
            "action, resource_type, namespace, resource_id, outcome, details, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.audit_id,
                event.correlation_id,
                event.tenant_id,
                event.actor,
                event.key_id,
                event.action,
                event.resource_type,
                event.namespace,
                event.resource_id,
                event.outcome,
                json.dumps(event.details, ensure_ascii=False) if event.details else None,
                event.timestamp,
            ),
        )

    def record(self, event: AuditEvent) -> str:
        """单独写入并提交一条审计记录，返回 audit_id。"""
        with self._db.transaction():
            self.put(event)
        return event.audit_id

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[AuditEvent]:
        rows = self._db.call(lambda: self._conn.execute(sql, params).fetchall())
        return [self._row_to_event(row) for row in rows]

    def query_by_correlation(self, correlation_id: str) -> list[AuditEvent]:
        """按 correlation_id 查询调用链，按时间升序排列。"""
        return self._query(
            "SELECT * FROM audit_events WHERE correlation_id = ? ORDER BY timestamp",
            (correlation_id,),
        )

    def query_by_tenant(self, tenant_id: str, limit: int = 100) -> list[AuditEvent]:
        """按租户查询审计记录，按时间降序排列。"""
        return self._query(
            "SELECT * FROM audit_events WHERE tenant_id = ? ORDER BY timestamp DESC LIMIT ?",
            (tenant_id, limit),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> AuditEvent:
        """将数据库行反序列化为 AuditEvent。"""
        return AuditEvent(
            audit_id=row["audit_id"],
            correlation_id=row["correlation_id"],
            tenant_id=row["tenant_id"],
            actor=row["actor"],
            key_id=row["key_id"],
            action=row["action"],
            resource_type=row["resource_type"],
            namespace=row["namespace"],
            resource_id=row["resource_id"],
            outcome=row["outcome"],
            details=json.loads(row["details"]) if row["details"] else None,
            timestamp=row["timestamp"],
        )

    def close(self) -> None:
        """关闭自己打开的数据库连接；共用的数据库由创建者关闭。"""
        if self._owns_db:
            self._db.close()


__all__ = ["AuditEvent", "AuditLog"]
