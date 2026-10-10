"""审计事件与关联 ID 追踪。

对齐 development-plan 5.5 / INV-4。审计事件记录关键操作，
correlation_id 全链路贯穿，支持调用链查询。

兼容 Python 3.9+。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS audit_events (
    audit_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id, timestamp);
"""


class AuditEvent(BaseModel):
    """审计事件。

    Attributes:
        audit_id: 审计记录唯一标识（UUID）。
        correlation_id: 关联 ID，全链路贯穿，用于调用链查询。
        tenant_id: 租户标识。
        actor: 操作发起者（用户/服务账号），可为 None。
        action: 操作类型（如 create/allocate/deploy）。
        resource_type: 资源类型。
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
    action: str
    resource_type: str
    resource_id: str
    outcome: str  # success / failure / denied
    details: dict[str, Any] | None = None
    timestamp: float = Field(default_factory=time.time)


class AuditLog:
    """审计日志（SQLite 后端）。

    Args:
        db_path: SQLite 数据库路径，默认 :memory:。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """创建表与索引（幂等）。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def record(self, event: AuditEvent) -> str:
        """记录审计事件，返回 audit_id。"""
        self._conn.execute(
            "INSERT INTO audit_events (audit_id, correlation_id, tenant_id, actor, action, "
            "resource_type, resource_id, outcome, details, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.audit_id,
                event.correlation_id,
                event.tenant_id,
                event.actor,
                event.action,
                event.resource_type,
                event.resource_id,
                event.outcome,
                json.dumps(event.details, ensure_ascii=False) if event.details else None,
                event.timestamp,
            ),
        )
        self._conn.commit()
        return event.audit_id

    def query_by_correlation(self, correlation_id: str) -> list[AuditEvent]:
        """按 correlation_id 查询调用链，按时间升序排列。"""
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE correlation_id = ? ORDER BY timestamp",
            (correlation_id,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def query_by_tenant(self, tenant_id: str, limit: int = 100) -> list[AuditEvent]:
        """按租户查询审计记录，按时间降序排列。"""
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE tenant_id = ? ORDER BY timestamp DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> AuditEvent:
        """将数据库行反序列化为 AuditEvent。"""
        return AuditEvent(
            audit_id=row["audit_id"],
            correlation_id=row["correlation_id"],
            tenant_id=row["tenant_id"],
            actor=row["actor"],
            action=row["action"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            outcome=row["outcome"],
            details=json.loads(row["details"]) if row["details"] else None,
            timestamp=row["timestamp"],
        )

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()


__all__ = ["AuditEvent", "AuditLog"]
