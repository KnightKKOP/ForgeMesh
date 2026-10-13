"""事务性 Outbox 与事件 Dispatcher。

对齐 ADR-008 与 architecture.md §12.3：业务写入与它的事件在同一个 SQLite 事务中提交
（:meth:`Outbox.put` 不自行提交），:class:`Dispatcher` 异步发布。

投递语义
--------
* **至少一次**：事件发布成功后才标记 ``published``；发布成功、标记之前进程退出时，
  重启后会再次发布。消费者须按 ``event_id`` 去重（:class:`IdempotentConsumer` 把已处理
  的事件记在 ``consumed_events`` 表中）。
* **状态只在数据库中**：``pending`` → ``published``；发布失败时记录失败次数
  ``attempts`` 与最近一次错误 ``last_error``，按指数退避推迟 ``next_attempt_at``
  （:class:`RetryPolicy`）；失败次数达到上限后进入 ``dead``，不再自动重试，可经
  :meth:`Outbox.redrive` 重新投递。Dispatcher 不在内存中记录已处理的事件，重启后
  不会重复发布已标记 ``published`` 的事件。
* **顺序**：按创建时间尽力有序。重试与重新投递会让后创建的事件先送达，消费者以
  资源版本（``resourceVersion``）判断新旧。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .sqlite import Database

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    published_at REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status, created_at);
"""

_CONSUMED_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS consumed_events (
    consumer TEXT NOT NULL,
    event_id TEXT NOT NULL,
    consumed_at REAL NOT NULL,
    PRIMARY KEY (consumer, event_id)
);
"""

#: v1.0.0 的 ``outbox_events`` 没有的列（打开存量库时补齐）。
_ADDED_COLUMNS = (
    ("attempts", "attempts INTEGER NOT NULL DEFAULT 0"),
    ("next_attempt_at", "next_attempt_at REAL"),
    ("last_error", "last_error TEXT"),
)

#: ``last_error`` 保存的最大字符数。
_MAX_ERROR_LENGTH = 500

#: 已发布事件默认保留 7 天，之后由 :meth:`Dispatcher.run` 删除。
DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600.0

_PURGE_INTERVAL_SECONDS = 3600.0
_MAX_ERROR_BACKOFF_SECONDS = 30.0


class OutboxStatus(StrEnum):
    """``outbox_events.status`` 的取值。"""

    PENDING = "pending"
    PUBLISHED = "published"
    DEAD = "dead"


class OutboxEvent(BaseModel):
    """Outbox 事件模型。

    Attributes:
        event_id: 事件唯一标识（UUID），消费者据此去重。
        aggregate_type: 聚合根类型（如 InferenceService）。
        aggregate_id: 聚合根标识。
        event_type: 事件类型（如 inference.service.created）。
        payload: 事件负载（JSON 可序列化字典），发布时原样送出。
        created_at: 事件创建时间戳。
        published_at: 事件发布时间戳，未发布时为 None。
        status: 事件状态（pending / published / dead）。
        attempts: 已失败的发布次数。
        next_attempt_at: ``pending`` 事件的下次发布时间戳；其余状态为 None。
        last_error: 最近一次发布失败的原因。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    published_at: float | None = None
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    next_attempt_at: float | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """发布失败后的重试策略。

    第 ``n`` 次失败后等待 ``min(base_delay * 2 ** (n - 1), max_delay)`` 秒再试；失败
    ``max_attempts`` 次后进入 ``dead``。默认值下两次尝试之间依次等待 1、2、4 … 256 秒，
    第一次失败约 8.5 分钟后放弃。

    Raises:
        ValueError: ``max_attempts`` 小于 1、``base_delay`` 不大于 0 或
            ``max_delay`` 小于 ``base_delay``。
    """

    max_attempts: int = 10
    base_delay: float = 1.0
    max_delay: float = 300.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay <= 0:
            raise ValueError(f"base_delay must be > 0, got {self.base_delay}")
        if self.max_delay < self.base_delay:
            raise ValueError(
                f"max_delay must be >= base_delay, got {self.max_delay} < {self.base_delay}"
            )

    def delay(self, attempts: int) -> float:
        """第 ``attempts`` 次失败之后等待的秒数。"""
        exponent = min(max(attempts - 1, 0), 63)  # 2.0 ** 很大的指数会溢出
        return min(self.base_delay * 2.0**exponent, self.max_delay)


class Outbox:
    """事务性 Outbox（SQLite 后端）。

    Outbox 模式核心：业务状态更新与事件写入在同一 SQLite 事务中完成，
    保证二者原子性。Dispatcher 异步轮询到期的 pending 事件并发布。

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
        """创建表与索引（幂等），并迁移存量库。"""
        with self._db.lock:
            self._conn.executescript(_SCHEMA_SQL)
            self._migrate_legacy_events()
            self._conn.commit()

    def _migrate_legacy_events(self) -> None:
        """存量库迁移（幂等）：补齐重试相关的列；v1.0.0 的 ``failed``（一次失败即永久
        放弃）改为 ``dead``，可经 :meth:`redrive` 重新投递；pending 事件以创建时间作为
        下次发布时间。"""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(outbox_events)")}
        for name, ddl in _ADDED_COLUMNS:
            if name not in columns:
                self._conn.execute(f"ALTER TABLE outbox_events ADD COLUMN {ddl}")
                logger.info("migrated outbox_events: added column %r", name)
        failed = self._conn.execute(
            "UPDATE outbox_events SET status = ? WHERE status = 'failed'",
            (OutboxStatus.DEAD.value,),
        ).rowcount
        if failed:
            logger.info("migrated outbox_events: %d legacy 'failed' events are now 'dead'", failed)
        self._conn.execute(
            "UPDATE outbox_events SET next_attempt_at = created_at "
            "WHERE status = ? AND next_attempt_at IS NULL",
            (OutboxStatus.PENDING.value,),
        )

    @property
    def database(self) -> Database:
        """事件表所在的数据库，供业务写入与事件共用同一个连接和锁。"""
        return self._db

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self._db.call(lambda: self._conn.execute(sql, params).fetchall())

    def put(self, event: OutboxEvent) -> None:
        """在调用方的事务中写入一个新的 pending 事件，不提交：事件随业务写入一起提交
        或回滚。``event`` 的状态与重试字段被忽略，事件立即到期。

        调用方须持有 :attr:`database` 的锁并已开启事务，例如在
        :meth:`Database.transaction <forgemesh.controlplane.sqlite.Database.transaction>`
        中，或在 :meth:`Database.call <forgemesh.controlplane.sqlite.Database.call>`
        执行的写入函数里、业务写入之后。

        Raises:
            RuntimeError: 连接上没有进行中的事务（事件不会写入）。
        """
        if not self._conn.in_transaction:
            raise RuntimeError("Outbox.put must run inside the caller's transaction")
        self._conn.execute(
            "INSERT INTO outbox_events (event_id, aggregate_type, aggregate_id, "
            "event_type, payload, created_at, status, attempts, next_attempt_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                event.event_id,
                event.aggregate_type,
                event.aggregate_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False),
                event.created_at,
                OutboxStatus.PENDING.value,
                event.created_at,
            ),
        )

    def get(self, event_id: str) -> OutboxEvent | None:
        """按 ``event_id`` 获取任意状态的事件；不存在（或已发布并被清理）时返回 None。"""
        rows = self._query("SELECT * FROM outbox_events WHERE event_id = ?", (event_id,))
        return self._row_to_event(rows[0]) if rows else None

    def get_pending(self, limit: int = 100) -> list[OutboxEvent]:
        """获取 pending 事件（含尚在退避中的），按创建时间升序排列。"""
        rows = self._query(
            "SELECT * FROM outbox_events WHERE status = ? ORDER BY created_at, event_id LIMIT ?",
            (OutboxStatus.PENDING.value, limit),
        )
        return [self._row_to_event(r) for r in rows]

    def due(self, limit: int = 100, now: float | None = None) -> list[OutboxEvent]:
        """获取已到发布时间的 pending 事件，按创建时间升序排列。

        Args:
            limit: 最多返回的事件数。
            now: 当前时间（Unix 秒），默认取系统时间。
        """
        at = time.time() if now is None else now
        rows = self._query(
            "SELECT * FROM outbox_events WHERE status = ? AND next_attempt_at <= ? "
            "ORDER BY created_at, event_id LIMIT ?",
            (OutboxStatus.PENDING.value, at, limit),
        )
        return [self._row_to_event(r) for r in rows]

    def list_dead(self, limit: int = 100) -> list[OutboxEvent]:
        """获取 ``dead`` 事件（含失败次数与最近一次错误），按创建时间升序排列。"""
        rows = self._query(
            "SELECT * FROM outbox_events WHERE status = ? ORDER BY created_at, event_id LIMIT ?",
            (OutboxStatus.DEAD.value, limit),
        )
        return [self._row_to_event(r) for r in rows]

    def mark_published(self, event_id: str, now: float | None = None) -> None:
        """标记事件已发布。

        仅当事件处于 pending 状态时才可标记；否则抛 ValueError。
        """
        at = time.time() if now is None else now

        def _mark() -> None:
            cur = self._conn.execute(
                "UPDATE outbox_events SET status = ?, published_at = ?, next_attempt_at = NULL "
                "WHERE event_id = ? AND status = ?",
                (OutboxStatus.PUBLISHED.value, at, event_id, OutboxStatus.PENDING.value),
            )
            if cur.rowcount == 0:
                raise ValueError(f"event not found or already published: {event_id}")
            self._conn.commit()

        self._db.call(_mark)

    def mark_failed(
        self,
        event_id: str,
        reason: str = "",
        *,
        policy: RetryPolicy | None = None,
        now: float | None = None,
    ) -> OutboxEvent:
        """记录一次发布失败：按 ``policy`` 推迟下次发布，失败次数达到上限时进入 ``dead``。

        Args:
            event_id: 事件标识。
            reason: 失败原因，截断到 500 个字符后存入 ``last_error``。
            policy: 重试策略，默认 :class:`RetryPolicy`。
            now: 当前时间（Unix 秒），默认取系统时间。

        Returns:
            更新后的事件。

        Raises:
            ValueError: 事件不存在或不处于 pending。
        """
        retry = policy or RetryPolicy()
        at = time.time() if now is None else now
        error = reason[:_MAX_ERROR_LENGTH] or None

        def _mark() -> OutboxEvent:
            row = self._conn.execute(
                "SELECT * FROM outbox_events WHERE event_id = ? AND status = ?",
                (event_id, OutboxStatus.PENDING.value),
            ).fetchone()
            if row is None:
                raise ValueError(f"event not found or not pending: {event_id}")
            attempts = row["attempts"] + 1
            if attempts >= retry.max_attempts:
                status, next_attempt_at = OutboxStatus.DEAD, None
            else:
                status, next_attempt_at = OutboxStatus.PENDING, at + retry.delay(attempts)
            self._conn.execute(
                "UPDATE outbox_events SET status = ?, attempts = ?, next_attempt_at = ?, "
                "last_error = ? WHERE event_id = ? AND status = ?",
                (status.value, attempts, next_attempt_at, error, event_id, row["status"]),
            )
            self._conn.commit()
            return self._row_to_event(row).model_copy(
                update={
                    "status": status,
                    "attempts": attempts,
                    "next_attempt_at": next_attempt_at,
                    "last_error": error,
                }
            )

        return self._db.call(_mark)

    def redrive(self, event_ids: Iterable[str] | None = None, *, now: float | None = None) -> int:
        """把 ``dead`` 事件放回待发布队列：失败次数与错误清零、立即到期。

        Args:
            event_ids: 只处理其中的 ``dead`` 事件；``None`` 表示全部 ``dead`` 事件。
                其他状态的事件不受影响。
            now: 当前时间（Unix 秒），默认取系统时间。

        Returns:
            重新放回队列的事件数。
        """
        at = time.time() if now is None else now
        ids = None if event_ids is None else list(dict.fromkeys(event_ids))
        if ids == []:
            return 0
        sql = (
            "UPDATE outbox_events SET status = ?, attempts = 0, next_attempt_at = ?, "
            "last_error = NULL WHERE status = ?"
        )
        params: list[Any] = [OutboxStatus.PENDING.value, at, OutboxStatus.DEAD.value]
        if ids is not None:
            sql += f" AND event_id IN ({', '.join('?' for _ in ids)})"
            params.extend(ids)

        def _redrive() -> int:
            count = self._conn.execute(sql, params).rowcount
            self._conn.commit()
            return count

        return self._db.call(_redrive)

    def purge_published(self, before: float) -> int:
        """删除发布时间早于 ``before``（Unix 秒）的已发布事件，返回删除条数。"""

        def _purge() -> int:
            count = self._conn.execute(
                "DELETE FROM outbox_events WHERE status = ? AND published_at < ?",
                (OutboxStatus.PUBLISHED.value, before),
            ).rowcount
            self._conn.commit()
            return count

        return self._db.call(_purge)

    def get_stats(self) -> dict[str, int]:
        """获取 Outbox 统计信息（各状态事件数量）。"""
        rows = self._query("SELECT status, COUNT(*) as cnt FROM outbox_events GROUP BY status")
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
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
        )

    async def close(self) -> None:
        """关闭数据库连接（共用的 ``database`` 由创建者关闭）。"""
        if self._owns_db:
            await asyncio.to_thread(self._db.close)


def _describe(exc: Exception) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class Dispatcher:
    """异步事件 Dispatcher：从数据库取出到期的 pending 事件，交给 publisher 发布。

    发布成功标记 ``published``；失败按 :class:`RetryPolicy` 推迟重试或进入 ``dead``，
    并结束本轮：发布目标不可用时，不会在一轮里对每个事件各等一次超时。是否已发布
    只看数据库状态，重启后不会重复发布已标记的事件。

    Args:
        outbox: Outbox 实例。
        publisher: 发布回调，签名 ``async (event: OutboxEvent) -> None``
            或同步 ``(event: OutboxEvent) -> None``；抛出异常表示发布失败，
            异常类型与消息记入 ``last_error`` 和日志，不得包含凭证。
        poll_interval: 两轮之间的间隔秒数。
        batch_size: 每轮最多发布的事件数。
        retry: 重试策略，默认 :class:`RetryPolicy`。
        retention: 已发布事件的保留秒数，:meth:`run` 每小时删除更早的事件；
            ``None`` 表示不删除。
        clock: 当前时间（Unix 秒），供测试注入。

    Raises:
        ValueError: ``poll_interval`` 不大于 0，或 ``batch_size`` 小于 1。
    """

    def __init__(
        self,
        outbox: Outbox,
        publisher: Callable[[OutboxEvent], Any],
        poll_interval: float = 1.0,
        batch_size: int = 100,
        *,
        retry: RetryPolicy | None = None,
        retention: float | None = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be > 0, got {poll_interval}")
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        self._outbox = outbox
        self._publisher = publisher
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._retry = retry or RetryPolicy()
        self._retention = retention
        self._clock = clock
        self._stopped = False
        self._wakeup: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    async def dispatch_once(self) -> int:
        """发布一轮到期事件，返回发布成功的事件数；遇到发布失败即结束本轮。"""
        events = await asyncio.to_thread(self._outbox.due, self._batch_size, self._clock())
        published = 0
        for event in events:
            if self._stopped:
                break
            try:
                result = self._publisher(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                failed = await asyncio.to_thread(
                    self._outbox.mark_failed,
                    event.event_id,
                    _describe(exc),
                    policy=self._retry,
                    now=self._clock(),
                )
                self._log_failure(failed)
                break
            await asyncio.to_thread(self._outbox.mark_published, event.event_id, self._clock())
            published += 1
        return published

    def _log_failure(self, event: OutboxEvent) -> None:
        if event.status == OutboxStatus.DEAD:
            logger.error(
                "outbox event %s (%s) is dead after %d failed attempts: %s",
                event.event_id,
                event.event_type,
                event.attempts,
                event.last_error,
            )
            return
        logger.warning(
            "outbox event %s (%s) publish failed (attempt %d of %d), retrying in %.0fs: %s",
            event.event_id,
            event.event_type,
            event.attempts,
            self._retry.max_attempts,
            self._retry.delay(event.attempts),
            event.last_error,
        )

    async def run(self) -> None:
        """持续分发，直到 :meth:`stop`。

        数据库错误不会结束循环：记录日志后按指数退避（最长 30 秒）再试。
        """
        self._wakeup = asyncio.Event()
        errors = 0
        next_purge = 0.0
        while not self._stopped:
            try:
                await self.dispatch_once()
                if self._retention is not None and time.monotonic() >= next_purge:
                    before = self._clock() - self._retention
                    await asyncio.to_thread(self._outbox.purge_published, before)
                    next_purge = time.monotonic() + _PURGE_INTERVAL_SECONDS
                errors = 0
                delay = self._poll_interval
            except Exception:
                errors += 1
                delay = min(self._poll_interval * 2**errors, _MAX_ERROR_BACKOFF_SECONDS)
                logger.exception("outbox dispatch failed; retrying in %.1fs", delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), delay)

    def stop(self) -> None:
        """停止轮询：正在进行的发布完成后，:meth:`run` 返回。"""
        self._stopped = True
        if self._wakeup is not None:
            self._wakeup.set()

    def start(self) -> asyncio.Task[None]:
        """在当前事件循环中以后台任务运行 :meth:`run`。

        Raises:
            RuntimeError: 已经启动过。
        """
        if self._task is not None:
            raise RuntimeError("dispatcher already started")
        self._task = asyncio.create_task(self.run(), name="outbox-dispatcher")
        return self._task

    async def shutdown(self, timeout: float = 10.0) -> None:
        """停止并等待 :meth:`start` 启动的任务结束；超过 ``timeout`` 秒则取消它。

        被取消时正在发布的事件尚未标记 ``published``，重启后会再次发布。
        """
        self.stop()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout)
        except TimeoutError:
            logger.warning("outbox dispatcher did not stop within %.1fs; cancelled", timeout)


class IdempotentConsumer:
    """幂等消费者：按 event_id 去重，已处理的事件记在 SQLite 中，进程重启后仍然有效。

    handler 正常返回后才记录事件：handler 抛出异常时不记录，再次投递时重新处理。
    handler 返回之后、记录之前进程退出时，重启后会再处理一次，因此有外部副作用的
    handler 仍应自身幂等。同一个实例的 :meth:`consume` 串行执行，同一事件不会被
    并发处理两次。

    Args:
        name: 消费者名称；不同名称各自去重，可共用一张表。
        db_path: SQLite 数据库路径，默认 :memory:（只在进程内有效）。
        database: 共用的数据库；给出时忽略 ``db_path``，:meth:`close` 不关闭它
            （由创建者关闭）。

    Raises:
        ValueError: ``name`` 为空。
    """

    def __init__(
        self,
        name: str = "default",
        db_path: str = ":memory:",
        *,
        database: Database | None = None,
    ) -> None:
        if not name:
            raise ValueError("consumer name must not be empty")
        self._name = name
        self._owns_db = database is None
        self._db = database if database is not None else Database(db_path)
        self._conn = self._db.conn
        self._consume_lock = threading.Lock()
        with self._db.lock:
            self._conn.executescript(_CONSUMED_SCHEMA_SQL)
            self._conn.commit()

    def is_processed(self, event_id: str) -> bool:
        """检查是否已处理过。"""
        row = self._db.call(
            lambda: self._conn.execute(
                "SELECT 1 FROM consumed_events WHERE consumer = ? AND event_id = ?",
                (self._name, event_id),
            ).fetchone()
        )
        return row is not None

    def consume(self, event: OutboxEvent, handler: Callable[[OutboxEvent], Any]) -> bool:
        """消费事件（幂等）。

        返回 True 表示首次处理，False 表示重复跳过；handler 的异常原样抛出，
        事件不记为已处理。
        """
        with self._consume_lock:
            if self.is_processed(event.event_id):
                return False
            handler(event)

            def _record() -> None:
                self._conn.execute(
                    "INSERT INTO consumed_events (consumer, event_id, consumed_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (consumer, event_id) DO NOTHING",
                    (self._name, event.event_id, time.time()),
                )
                self._conn.commit()

            self._db.call(_record)
            return True

    @property
    def processed_count(self) -> int:
        """已处理事件数。"""
        row = self._db.call(
            lambda: self._conn.execute(
                "SELECT COUNT(*) FROM consumed_events WHERE consumer = ?", (self._name,)
            ).fetchone()
        )
        return int(row[0])

    def close(self) -> None:
        """关闭数据库连接（共用的 ``database`` 由创建者关闭）。"""
        if self._owns_db:
            self._db.close()


__all__ = [
    "DEFAULT_RETENTION_SECONDS",
    "Dispatcher",
    "IdempotentConsumer",
    "Outbox",
    "OutboxEvent",
    "OutboxStatus",
    "RetryPolicy",
]
