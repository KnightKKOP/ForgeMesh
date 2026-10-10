"""决策记录模型与 SQLite 持久化（对齐 development-plan 5.5 / IVT-11）。

决策记录用于审计回溯：每次决策请求的候选、选中结果、风险等级与审批状态
均被持久化。审批集成通过 ``approval_status`` 字段实现：

* ``None`` —— 无需审批；
* ``"pending"`` —— 待审批；
* ``"approved"`` —— 已通过；
* ``"rejected"`` —— 已拒绝。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from forgemesh.core.errors import NotFoundError
from forgemesh.decision.provider import DecisionCandidate
from pydantic import BaseModel, ConfigDict, Field

#: 建表与建索引 DDL（幂等执行）。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_records (
    record_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    operation TEXT NOT NULL,
    candidates TEXT NOT NULL,
    chosen TEXT,
    success INTEGER NOT NULL,
    risk_level TEXT,
    created_at REAL NOT NULL,
    approval_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_tenant ON decision_records(tenant_id);
CREATE INDEX IF NOT EXISTS idx_records_approval ON decision_records(tenant_id, approval_status);
CREATE INDEX IF NOT EXISTS idx_records_request ON decision_records(request_id);
"""

#: 审批状态常量。
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"


class DecisionRecord(BaseModel):
    """决策记录模型。

    Attributes:
        record_id: 记录唯一标识。
        request_id: 关联的 DecisionRequest 标识。
        tenant_id: 租户标识。
        provider_id: 决策 Provider 标识。
        provider_version: Provider 版本。
        operation: 操作语义标识。
        candidates: 候选列表。
        chosen: 选中的候选（未选中时为 ``None``）。
        success: 决策是否成功。
        risk_level: 风险等级（low/medium/high）。
        created_at: 创建时间戳（Unix 秒）。
        approval_status: 审批状态（``None`` / ``"pending"`` / ``"approved"`` / ``"rejected"``）。
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str
    request_id: str
    tenant_id: str
    provider_id: str
    provider_version: str
    operation: str
    candidates: list[DecisionCandidate] = Field(default_factory=list)
    chosen: DecisionCandidate | None = None
    success: bool = False
    risk_level: str | None = None
    created_at: float = Field(default_factory=time.time)
    approval_status: str | None = None


class DecisionRecordStore:
    """基于 SQLite 的决策记录持久化存储。

    Args:
        db_path: SQLite 数据库文件路径，默认 ``:memory:``。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _init_schema(self) -> None:
        """创建表与索引（幂等）。"""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _serialize_candidates(candidates: list[DecisionCandidate]) -> str:
        """序列化候选列表为 JSON。"""
        return json.dumps(
            [c.model_dump() for c in candidates], ensure_ascii=False
        )

    @staticmethod

    def _serialize_chosen(chosen: DecisionCandidate | None) -> str | None:
        """序列化选中候选为 JSON。"""
        if chosen is None:
            return None
        return chosen.model_dump_json()

    @staticmethod
    def _deserialize_chosen(raw: str | None) -> DecisionCandidate | None:
        """从 JSON 反序列化选中候选。"""
        if raw is None:
            return None
        return DecisionCandidate.model_validate_json(raw)

    def _row_to_record(self, row: sqlite3.Row) -> DecisionRecord:
        """将数据库行转换为 DecisionRecord。"""
        return DecisionRecord(
            record_id=row["record_id"],
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            provider_id=row["provider_id"],
            provider_version=row["provider_version"],
            operation=row["operation"],
            candidates=[
                DecisionCandidate.model_validate(item)
                for item in json.loads(row["candidates"])
            ],
            chosen=self._deserialize_chosen(row["chosen"]),
            success=bool(row["success"]),
            risk_level=row["risk_level"],
            created_at=row["created_at"],
            approval_status=row["approval_status"],
        )

    # ------------------------------------------------------------------ #
    # 同步实现
    # ------------------------------------------------------------------ #

    def _save_sync(self, record: DecisionRecord) -> None:
        """同步保存决策记录。"""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO decision_records (
                record_id, request_id, tenant_id, provider_id,
                provider_version, operation, candidates, chosen,
                success, risk_level, created_at, approval_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.request_id,
                record.tenant_id,
                record.provider_id,
                record.provider_version,
                record.operation,
                self._serialize_candidates(record.candidates),
                self._serialize_chosen(record.chosen),
                int(record.success),
                record.risk_level,
                record.created_at,
                record.approval_status,
            ),
        )
        self._conn.commit()

    def _get_sync(self, record_id: str) -> DecisionRecord:
        """同步查询单条记录。"""
        row = self._conn.execute(
            "SELECT * FROM decision_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"decision record not found: {record_id}")
        return self._row_to_record(row)

    def _list_by_tenant_sync(
        self, tenant_id: str, limit: int = 100
    ) -> list[DecisionRecord]:
        """同步按租户列出记录。"""
        rows = self._conn.execute(
            "SELECT * FROM decision_records WHERE tenant_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _list_pending_approvals_sync(
        self, tenant_id: str
    ) -> list[DecisionRecord]:
        """同步列出待审批记录。"""
        rows = self._conn.execute(
            "SELECT * FROM decision_records "
            "WHERE tenant_id = ? AND approval_status = ? "
            "ORDER BY created_at DESC",
            (tenant_id, APPROVAL_PENDING),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _update_approval_status_sync(
        self, record_id: str, status: str
    ) -> None:
        """同步更新审批状态。"""
        row = self._conn.execute(
            "SELECT 1 FROM decision_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"decision record not found: {record_id}")
        self._conn.execute(
            "UPDATE decision_records SET approval_status = ? WHERE record_id = ?",
            (status, record_id),
        )
        self._conn.commit()

    def _close_sync(self) -> None:
        """同步关闭连接。"""
        self._conn.close()

    # ------------------------------------------------------------------ #
    # 公开异步 API
    # ------------------------------------------------------------------ #

    async def save(self, record: DecisionRecord) -> None:
        """保存决策记录（存在则覆盖）。"""
        await asyncio.to_thread(self._save_sync, record)

    async def get(self, record_id: str) -> DecisionRecord:
        """查询单条决策记录。

        Raises:
            NotFoundError: 记录不存在。
        """
        return await asyncio.to_thread(self._get_sync, record_id)

    async def list_by_tenant(
        self, tenant_id: str, limit: int = 100
    ) -> list[DecisionRecord]:
        """按租户列出决策记录（按创建时间降序）。"""
        return await asyncio.to_thread(
            self._list_by_tenant_sync, tenant_id, limit
        )

    async def list_pending_approvals(
        self, tenant_id: str
    ) -> list[DecisionRecord]:
        """列出指定租户的待审批决策记录。"""
        return await asyncio.to_thread(
            self._list_pending_approvals_sync, tenant_id
        )

    async def update_approval_status(
        self, record_id: str, status: str
    ) -> None:
        """更新决策记录的审批状态。

        Args:
            record_id: 记录标识。
            status: 新审批状态（``"pending"`` / ``"approved"`` / ``"rejected"``）。

        Raises:
            NotFoundError: 记录不存在。
        """
        await asyncio.to_thread(
            self._update_approval_status_sync, record_id, status
        )

    async def close(self) -> None:
        """关闭数据库连接。"""
        await asyncio.to_thread(self._close_sync)


__all__ = [
    "DecisionRecord",
    "DecisionRecordStore",
    "APPROVAL_PENDING",
    "APPROVAL_APPROVED",
    "APPROVAL_REJECTED",
]
