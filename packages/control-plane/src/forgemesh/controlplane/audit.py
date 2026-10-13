"""审计记录与关联 ID 追踪。

对齐 development-plan 5.5 / INV-4。审计记录关键操作，
correlation_id 全链路贯穿，支持调用链查询。

资源注册表在 API 写入资源的同一个事务中写入审计记录（:meth:`AuditLog.put`），记录调用
主体（租户、主体、凭证 id）与关联 ID：资源写入失败时不会留下审计记录。
:meth:`AuditLog.record` 单独写入并提交一条记录。:meth:`AuditLog.list_page` 按租户分页
列出记录（``GET /api/v1/audit-events``），游标与资源列表的游标一样签名
（:mod:`forgemesh.controlplane.pagetokens`）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .pagetokens import PageTokenSigner, load_page_token_key, page_scope
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
CREATE INDEX IF NOT EXISTS idx_audit_tenant_correlation
    ON audit_events(tenant_id, correlation_id, timestamp);
"""

#: v1.0.0 的 ``audit_events`` 没有的列（打开存量库时补齐）。
_ADDED_COLUMNS = (
    ("key_id", "key_id TEXT"),
    ("namespace", "namespace TEXT"),
)

#: 审计记录列表的游标位置：上一页最后一条记录的时间戳与 ``audit_id``。
_PAGE_POSITION = {"ts": float, "id": str}


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


class AuditPage(BaseModel):
    """一页审计记录。

    Attributes:
        items: 本页的记录，最新的在前。
        next_page_token: 取下一页的令牌；没有下一页时为 None。
    """

    model_config = ConfigDict(extra="forbid")

    items: list[AuditEvent]
    next_page_token: str | None = None


class AuditLog:
    """审计日志（SQLite 后端）。

    分页游标的签名密钥取自数据库的 ``registry_meta`` 表（没有时生成），与资源注册表共用
    数据库时即注册表的密钥。

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
        self._page_tokens = PageTokenSigner(self._db.call(load_page_token_key, self._conn))

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

    def list_page(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
        page_token: str | None = None,
        correlation_id: str | None = None,
    ) -> AuditPage:
        """按租户分页列出审计记录：最新的在前，时间相同时按 ``audit_id`` 降序。

        下一页从上一页最后一条记录的位置继续，翻页期间写入的新记录排在已经取过的位置之前，
        不会让后面的页重复或遗漏记录。游标（格式见 :mod:`forgemesh.controlplane.pagetokens`）
        记录这个位置并绑定这组查询条件（``limit`` 每页可以不同），不是访问凭据：查询始终
        限定在 ``tenant_id`` 内。

        Args:
            tenant_id: 只列出该租户的记录。
            limit: 单页最多返回的条数（至少为 1）。
            page_token: 上一页的 ``next_page_token``；为空时从最新的记录开始。
            correlation_id: 给出时只列出该关联 ID 的记录。

        Raises:
            ValueError: ``limit`` 小于 1。
            InvalidPageTokenError: 游标无法解析、被篡改，或签发给了另一个列表、另一个租户或
                另一个 ``correlation_id``（见 :mod:`forgemesh.controlplane.pagetokens`）。
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        scope = page_scope(
            {"list": "audit-events", "tenant": tenant_id, "correlationId": correlation_id}
        )
        sql = "SELECT * FROM audit_events WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if correlation_id is not None:
            sql += " AND correlation_id = ?"
            params.append(correlation_id)
        if page_token is not None:
            timestamp, audit_id = self._page_tokens.decode(page_token, scope, _PAGE_POSITION)
            sql += " AND timestamp <= ? AND (timestamp < ? OR audit_id < ?)"
            params += [timestamp, timestamp, audit_id]
        sql += " ORDER BY timestamp DESC, audit_id DESC LIMIT ?"
        params.append(limit + 1)
        events = self._query(sql, tuple(params))
        if len(events) <= limit:
            return AuditPage(items=events)
        items = events[:limit]
        position = {"ts": items[-1].timestamp, "id": items[-1].audit_id}
        return AuditPage(items=items, next_page_token=self._page_tokens.encode(position, scope))

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


__all__ = ["AuditEvent", "AuditLog", "AuditPage"]
