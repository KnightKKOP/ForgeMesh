"""幂等去重键生成与持久化。

对齐 development-plan 5.4：dedup_key 由 kind/namespace/name/uid/generation/operation 派生，
同一键的协调不得重复产生副作用。

:class:`ProcessedKeyStore` 将已处理去重键持久化到 SQLite（对齐 architecture
§5.5「不能依赖单进程内存状态保证全局正确性」）：进程重启后已处理键仍然
可见，避免重启后重复执行有副作用的协调动作。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from ..sqlite import connect


class DedupKey(BaseModel):
    """幂等去重键。

    Attributes:
        key: SHA-256 摘要（去重比对依据）。
        resource_kind: 资源类型（审计/诊断用）。
        resource_name: 资源名（审计/诊断用）。
        generation: 期望状态版本（审计/诊断用）。
        operation: 操作语义标识（审计/诊断用）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    resource_kind: str
    resource_name: str
    generation: int
    operation: str


def compute_dedup_key(
    kind: str,
    namespace: str | None,
    name: str,
    uid: str | None,
    generation: int,
    operation: str,
) -> DedupKey:
    """计算幂等去重键（SHA-256 摘要）。

    将 ``kind|namespace|name|uid|generation|operation`` 拼接为原始串，
    取 UTF-8 编码的 SHA-256 十六进制摘要作为 ``key``。相同输入必然产生
    相同 ``key``；任一字段不同则 ``key`` 不同（抗碰撞）。

    Args:
        kind: 资源类型标识。
        namespace: 命名空间（可为 ``None``）。
        name: 资源名。
        uid: 资源稳定标识（可为 ``None``）。
        generation: 期望状态版本。
        operation: 操作语义标识。

    Returns:
        :class:`DedupKey` 实例。
    """
    raw = f"{kind}|{namespace or ''}|{name}|{uid or ''}|{generation}|{operation}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return DedupKey(
        key=digest,
        resource_kind=kind,
        resource_name=name,
        generation=generation,
        operation=operation,
    )


__all__ = ["DedupKey", "ProcessedKeyStore", "compute_dedup_key"]


#: 去重键默认保留时长（24 小时）。
DEFAULT_DEDUP_TTL_SECONDS = 24 * 3600.0

#: 两次删除过期键之间的最长间隔（秒）；实际间隔不超过 TTL。
PURGE_INTERVAL_SECONDS = 60.0

#: 建表与建索引 DDL（幂等执行，表模式参照 registry/store.py）。
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS processed_dedup_keys (
    dedup_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_keys_created_at
    ON processed_dedup_keys(created_at);
"""


def _utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（固定微秒宽度）。

    ``timespec="microseconds"`` 保证字符串宽度固定，使 ``created_at`` 的
    字典序与时间序一致（SQLite TEXT 比较即可用于 TTL 过滤）。
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ProcessedKeyStore:
    """调和去重键的 SQLite 持久化存储（对齐 architecture §5.5：不依赖单进程内存状态）。

    表结构：``processed_dedup_keys(dedup_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)``。
    异步方法通过 :func:`asyncio.to_thread` 包装同步 sqlite3 调用（包装风格
    参照 ``registry/store.py``）；同步临界区以 :class:`threading.Lock` 串行化，
    避免同一连接被并发线程交错使用（Python 3.12 sqlite3 对同连接跨线程
    事务交错会抛 ``InterfaceError``）。

    TTL：``created_at`` 早于 ``now - ttl_seconds`` 的键视为过期，``seen`` 不再报告
    它们，默认保留 24 小时、可配置。过期键在 ``seen`` / ``mark`` 时**惰性删除**，
    两次删除之间至少间隔 ``min(ttl_seconds, PURGE_INTERVAL_SECONDS)``：``seen``
    平时只读，不必每次调用都写一次库。重复 ``mark`` 同一键幂等，且刷新
    ``created_at``（TTL 从最近一次处理起算）。

    Args:
        db_path: SQLite 数据库文件路径，默认 ``:memory:``（内存库，单测友好）；
            传入文件路径即获得跨进程重启的持久化语义。
        ttl_seconds: 去重键保留时长（秒），默认 24 小时。
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        ttl_seconds: float = DEFAULT_DEDUP_TTL_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._ttl_seconds = ttl_seconds
        self._purge_interval = min(ttl_seconds, PURGE_INTERVAL_SECONDS)
        # time.monotonic() 时刻；0 表示第一次 seen / mark 就清理上次运行留下的过期键。
        self._next_purge = 0.0
        self._conn = connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        """创建表与索引（幂等）。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def _cutoff(self) -> str:
        """未过期键的最早 ``created_at``。"""
        expiry = datetime.now(UTC) - timedelta(seconds=self._ttl_seconds)
        return expiry.isoformat(timespec="microseconds")

    def _purge_expired_if_due_unlocked(self) -> None:
        """距上次删除已满一个间隔时删除过期键（调用方须已持有 ``self._lock``）。"""
        now = time.monotonic()
        if now < self._next_purge:
            return
        self._next_purge = now + self._purge_interval
        self._conn.execute(
            "DELETE FROM processed_dedup_keys WHERE created_at < ?",
            (self._cutoff(),),
        )
        self._conn.commit()

    async def seen(self, key: str) -> bool:
        """查询去重键是否已处理且未过期（到期时顺带删除过期键）。

        Args:
            key: 去重键（SHA-256 摘要）。

        Returns:
            ``True`` 表示该键已处理且未过期；``False`` 表示未见或已过期。
        """

        def _seen() -> bool:
            with self._lock:
                self._purge_expired_if_due_unlocked()
                row = self._conn.execute(
                    "SELECT 1 FROM processed_dedup_keys WHERE dedup_key = ? AND created_at >= ?",
                    (key, self._cutoff()),
                ).fetchone()
                return row is not None

        return await asyncio.to_thread(_seen)

    async def mark(self, key: str) -> None:
        """标记去重键为已处理（幂等，到期时顺带删除过期键）。

        Args:
            key: 去重键（SHA-256 摘要）。
        """

        def _mark() -> None:
            with self._lock:
                self._purge_expired_if_due_unlocked()
                self._conn.execute(
                    "INSERT OR REPLACE INTO processed_dedup_keys "
                    "(dedup_key, created_at) VALUES (?, ?)",
                    (key, _utc_now_iso()),
                )
                self._conn.commit()

        await asyncio.to_thread(_mark)

    async def close(self) -> None:
        """关闭底层 SQLite 连接。"""

        def _close() -> None:
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(_close)
