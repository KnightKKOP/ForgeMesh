"""基础 Token 计量：追加式使用记录与聚合统计（对齐 development-plan WP-4.2）。

设计约束：

* **追加式**（IVT-10）：记录只插入不更新；计量修正通过新增 ``correction_of``
  记录实现，原记录始终保留，保证审计可追溯。
* **多租户隔离**：所有查询按 ``tenant_id`` 过滤，跨租户访问视为不存在。
* **异步非阻塞**：SQLite 操作通过 ``asyncio.to_thread`` 在线程池执行，
  避免阻塞事件循环。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import sqlite3

from forgemesh.core.errors import ConflictError, NotFoundError
from pydantic import BaseModel, ConfigDict

# SQLite 建表脚本：record_id 为主键，追加式写入靠主键约束保证幂等。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    record_id     TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    success       INTEGER NOT NULL,
    latency_ms    REAL NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at    REAL NOT NULL,
    correction_of TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_time
    ON usage_records(tenant_id, created_at);
"""


class UsageRecord(BaseModel):
    """单次使用记录（追加式，不可修改）。

    Attributes:
        record_id: 唯一标识。
        tenant_id: 租户标识。
        request_id: 关联请求标识。
        model_id: 模型标识。
        input_tokens: 输入 Token 数。
        output_tokens: 输出 Token 数。
        cached_tokens: 缓存命中 Token 数。
        success: 请求是否成功。
        latency_ms: 延迟（毫秒）。
        correlation_id: 关联追踪标识。
        created_at: 创建时间戳（Unix 秒）。
        correction_of: 修正记录指向的原记录 ID（IVT-10），``None`` 表示原始记录。
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str
    tenant_id: str
    request_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    success: bool
    latency_ms: float
    correlation_id: str
    created_at: float
    correction_of: str | None = None


class UsageSummary(BaseModel):
    """使用记录聚合统计。

    Attributes:
        tenant_id: 租户标识。
        total_input_tokens: 输入 Token 总数。
        total_output_tokens: 输出 Token 总数。
        total_cached_tokens: 缓存命中 Token 总数。
        success_count: 成功记录数。
        failure_count: 失败记录数。
        total_latency_ms: 延迟总和（毫秒）。
        avg_latency_ms: 平均延迟（毫秒），无记录时为 0.0。
        record_count: 记录总数。
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    record_count: int = 0


class UsageStore:
    """追加式使用记录存储（SQLite）。

    所有 SQLite 操作通过 ``asyncio.to_thread`` 在线程池执行，避免阻塞事件循环。
    记录只插入不更新；重复 ``record_id`` 抛 :class:`ConflictError`（追加式语义）。

    Args:
        db_path: SQLite 数据库路径，默认 ``":memory:"``（内存库）。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        # check_same_thread=False：允许 asyncio.to_thread 在不同线程访问同一连接。
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # record
    # ------------------------------------------------------------------ #

    async def record(self, record: UsageRecord) -> None:
        """追加一条使用记录（不可修改已有记录）。

        Raises:
            ConflictError: ``record_id`` 已存在（追加式约束）。
        """
        await asyncio.to_thread(self._record_sync, record)

    def _record_sync(self, record: UsageRecord) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO usage_records
                    (record_id, tenant_id, request_id, model_id,
                     input_tokens, output_tokens, cached_tokens,
                     success, latency_ms, correlation_id, created_at, correction_of)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.tenant_id,
                    record.request_id,
                    record.model_id,
                    record.input_tokens,
                    record.output_tokens,
                    record.cached_tokens,
                    int(record.success),
                    record.latency_ms,
                    record.correlation_id,
                    record.created_at,
                    record.correction_of,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"usage record already exists: {record.record_id}",
                details={"record_id": record.record_id},
            ) from exc

    # ------------------------------------------------------------------ #
    # get
    # ------------------------------------------------------------------ #

    async def get(self, record_id: str) -> UsageRecord:
        """查询单条使用记录。

        Raises:
            NotFoundError: 记录不存在。
        """
        return await asyncio.to_thread(self._get_sync, record_id)

    def _get_sync(self, record_id: str) -> UsageRecord:
        row = self._conn.execute(
            "SELECT * FROM usage_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"usage record not found: {record_id}")
        return self._row_to_record(row)

    # ------------------------------------------------------------------ #
    # list_by_tenant
    # ------------------------------------------------------------------ #

    async def list_by_tenant(
        self,
        tenant_id: str,
        start: float | None = None,
        end: float | None = None,
        limit: int = 100,
    ) -> list[UsageRecord]:
        """按租户查询使用记录，按 ``created_at`` 升序返回。

        Args:
            tenant_id: 租户标识。
            start: 起始时间戳（包含），``None`` 表示不限制下界。
            end: 结束时间戳（不包含），``None`` 表示不限制上界。
            limit: 最大返回条数。
        """
        return await asyncio.to_thread(
            self._list_by_tenant_sync, tenant_id, start, end, limit
        )

    def _list_by_tenant_sync(
        self,
        tenant_id: str,
        start: float | None,
        end: float | None,
        limit: int,
    ) -> list[UsageRecord]:
        query = "SELECT * FROM usage_records WHERE tenant_id = ?"
        params: list[str | float | int] = [tenant_id]
        if start is not None:
            query += " AND created_at >= ?"
            params.append(start)
        if end is not None:
            query += " AND created_at < ?"
            params.append(end)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    # ------------------------------------------------------------------ #
    # aggregate
    # ------------------------------------------------------------------ #

    async def aggregate(
        self, tenant_id: str, start: float, end: float
    ) -> UsageSummary:
        """聚合统计指定租户在 ``[start, end)`` 时间范围内的使用记录。

        无记录时返回各字段为零的空统计。
        """
        return await asyncio.to_thread(
            self._aggregate_sync, tenant_id, start, end
        )

    def _aggregate_sync(
        self, tenant_id: str, start: float, end: float
    ) -> UsageSummary:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                    AS record_count,
                COALESCE(SUM(input_tokens), 0)              AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0)             AS total_output_tokens,
                COALESCE(SUM(cached_tokens), 0)             AS total_cached_tokens,
                COALESCE(SUM(success), 0)                   AS success_count,
                COALESCE(SUM(1 - success), 0)               AS failure_count,
                COALESCE(SUM(latency_ms), 0.0)              AS total_latency_ms
            FROM usage_records
            WHERE tenant_id = ? AND created_at >= ? AND created_at < ?
            """,
            (tenant_id, start, end),
        ).fetchone()
        record_count = row["record_count"]
        total_latency = row["total_latency_ms"]
        return UsageSummary(
            tenant_id=tenant_id,
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
            total_cached_tokens=row["total_cached_tokens"],
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            total_latency_ms=total_latency,
            avg_latency_ms=(
                total_latency / record_count if record_count > 0 else 0.0
            ),
            record_count=record_count,
        )

    # ------------------------------------------------------------------ #
    # correct — 计量修正（IVT-10）
    # ------------------------------------------------------------------ #

    async def correct(
        self, original_id: str, correction: UsageRecord
    ) -> None:
        """计量修正：新增一条 ``correction_of`` 记录指向原记录，原记录保留。

        修正记录使用自身的 ``record_id``（须唯一），其 ``correction_of``
        被设置为 ``original_id``。

        Raises:
            NotFoundError: 原记录不存在。
            ConflictError: 修正记录的 ``record_id`` 已存在。
        """
        await asyncio.to_thread(self._correct_sync, original_id, correction)

    def _correct_sync(self, original_id: str, correction: UsageRecord) -> None:
        existing = self._conn.execute(
            "SELECT record_id FROM usage_records WHERE record_id = ?",
            (original_id,),
        ).fetchone()
        if existing is None:
            raise NotFoundError(
                f"original usage record not found: {original_id}"
            )
        correction.correction_of = original_id
        self._record_sync(correction)

    # ------------------------------------------------------------------ #
    # close
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """关闭数据库连接。"""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> UsageRecord:
        return UsageRecord(
            record_id=row["record_id"],
            tenant_id=row["tenant_id"],
            request_id=row["request_id"],
            model_id=row["model_id"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cached_tokens=row["cached_tokens"],
            success=bool(row["success"]),
            latency_ms=row["latency_ms"],
            correlation_id=row["correlation_id"],
            created_at=row["created_at"],
            correction_of=row["correction_of"],
        )


__all__ = [
    "UsageRecord",
    "UsageStore",
    "UsageSummary",
]
