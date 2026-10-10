"""事务性 Outbox 与事件 Dispatcher。

对齐 development-plan 5.5。Outbox 模式保证状态更新与事件发布的原子性：
同一 SQLite 事务中写入业务状态和事件记录，Dispatcher 异步发布。

幂等保证：消费者按 event_id 去重，重复投递不产生重复副作用。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    published_at REAL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status, created_at);
"""


class OutboxEvent(BaseModel):
    """Outbox 事件模型。

    Attributes:
        event_id: 事件唯一标识（UUID），消费者据此去重。
        aggregate_type: 聚合根类型（如 InferenceService）。
        aggregate_id: 聚合根标识。
        event_type: 事件类型（如 Created/Updated/Deleted）。
        payload: 事件负载（JSON 可序列化字典）。
        created_at: 事件创建时间戳。
        published_at: 事件发布时间戳，未发布时为 None。
        status: 事件状态（pending/published/failed）。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    published_at: Optional[float] = None
    status: str = "pending"


class Outbox:
    """事务性 Outbox（SQLite 后端）。

    Outbox 模式核心：业务状态更新与事件写入在同一 SQLite 事务中完成，
    保证二者原子性。Dispatcher 异步轮询 pending 事件并发布。

    Args:
        db_path: SQLite 数据库路径，默认 :memory:。
        conn: 可选的外部 SQLite 连接（用于与业务事务共享）。
    """

    def __init__(self, db_path: str = ":memory:", conn: Optional[sqlite3.Connection] = None) -> None:
        self._db_path = db_path
        self._conn = conn or sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """创建表与索引（幂等）。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        """暴露连接，供业务事务共享。"""
        return self._conn

    def put(self, event: OutboxEvent) -> None:
        """写入事件（同步，应在业务事务中调用）。

        与业务状态更新在同一事务中执行，保证原子性。
        """
        self._conn.execute(
            "INSERT INTO outbox_events (event_id, aggregate_type, aggregate_id, "
            "event_type, payload, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.aggregate_type,
                event.aggregate_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False),
                event.created_at,
                event.status,
            ),
        )
        self._conn.commit()

    def get_pending(self, limit: int = 100) -> List[OutboxEvent]:
        """获取待发布事件，按创建时间升序排列。"""
        rows = self._conn.execute(
            "SELECT * FROM outbox_events WHERE status = 'pending' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def mark_published(self, event_id: str) -> None:
        """标记事件已发布。

        仅当事件处于 pending 状态时才可标记；否则抛 ValueError。
        """
        cur = self._conn.execute(
            "UPDATE outbox_events SET status = 'published', published_at = ? "
            "WHERE event_id = ? AND status = 'pending'",
            (time.time(), event_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"event not found or already published: {event_id}")
        self._conn.commit()

    def mark_failed(self, event_id: str, reason: str = "") -> None:
        """标记事件发布失败。

        Args:
            event_id: 事件标识。
            reason: 失败原因（保留用于未来扩展，当前未持久化）。
        """
        self._conn.execute(
            "UPDATE outbox_events SET status = 'failed' WHERE event_id = ? AND status = 'pending'",
            (event_id,),
        )
        self._conn.commit()

    def get_stats(self) -> Dict[str, int]:
        """获取 Outbox 统计信息（各状态事件数量）。"""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) as cnt FROM outbox_events GROUP BY status"
        ).fetchall()
        return {row["status"]: row["cnt"] for row in rows}

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> OutboxEvent:
        """将数据库行反序列化为 OutboxEvent。"""
        return OutboxEvent(
            event_id=row["event_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            published_at=row["published_at"],
            status=row["status"],
        )

    async def close(self) -> None:
        """关闭数据库连接。"""
        await asyncio.to_thread(self._conn.close)


class Dispatcher:
    """异步事件 Dispatcher：轮询 Outbox 并发布。

    从 Outbox 取出 pending 事件，调用 publisher 回调发布，成功后标记 published，
    失败则标记 failed。已处理的 event_id 不会重复处理（幂等）。

    Args:
        outbox: Outbox 实例。
        publisher: 发布回调，签名 ``async (event: OutboxEvent) -> None``
            或同步 ``(event: OutboxEvent) -> None``。
        poll_interval: 轮询间隔秒数。
        batch_size: 每批获取事件数。
    """

    def __init__(
        self,
        outbox: Outbox,
        publisher: Callable[[OutboxEvent], Any],
        poll_interval: float = 1.0,
        batch_size: int = 100,
    ) -> None:
        self._outbox = outbox
        self._publisher = publisher
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._running = False
        self._processed: Set[str] = set()  # 已处理的 event_id（幂等）

    async def dispatch_once(self) -> int:
        """执行一轮分发，返回处理的事件数。"""
        events = await asyncio.to_thread(self._outbox.get_pending, self._batch_size)
        count = 0
        for event in events:
            if event.event_id in self._processed:
                continue  # 幂等：跳过已处理
            try:
                result = self._publisher(event)
                if asyncio.iscoroutine(result):
                    await result
                await asyncio.to_thread(self._outbox.mark_published, event.event_id)
                self._processed.add(event.event_id)
                count += 1
            except Exception:
                await asyncio.to_thread(self._outbox.mark_failed, event.event_id)
        return count

    async def run(self) -> None:
        """持续轮询分发（阻塞，直到 stop）。"""
        self._running = True
        while self._running:
            await self.dispatch_once()
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        """停止轮询。"""
        self._running = False


class IdempotentConsumer:
    """幂等消费者：按 event_id 去重。

    重复投递同一 event_id 的事件不会重复执行 handler。
    """

    def __init__(self) -> None:
        self._processed_ids: Set[str] = set()

    def is_processed(self, event_id: str) -> bool:
        """检查是否已处理过。"""
        return event_id in self._processed_ids

    def consume(self, event: OutboxEvent, handler: Callable[[OutboxEvent], Any]) -> bool:
        """消费事件（幂等）。

        返回 True 表示首次处理，False 表示重复跳过。
        """
        if event.event_id in self._processed_ids:
            return False
        handler(event)
        self._processed_ids.add(event.event_id)
        return True

    @property
    def processed_count(self) -> int:
        """已处理事件数。"""
        return len(self._processed_ids)


__all__ = ["Outbox", "OutboxEvent", "Dispatcher", "IdempotentConsumer"]
