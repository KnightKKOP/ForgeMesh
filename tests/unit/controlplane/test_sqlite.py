"""控制平面 SQLite 连接设置单元测试。

覆盖：

* ``connect`` 启用 WAL 与外键约束，``synchronous`` 保持 ``FULL``，等锁 5 秒；
* 控制平面的各个存储都以 WAL 模式打开数据库文件；
* 同一文件上一个连接的读事务不阻塞另一个存储写入。
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import time

import pytest
from forgemesh.controlplane.audit import AuditLog
from forgemesh.controlplane.outbox import Outbox
from forgemesh.controlplane.reconciler import ProcessedKeyStore
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.controlplane.sqlite import connect
from forgemesh.controlplane.tasks import TaskStore


def _close(store) -> None:
    """关闭存储（兼容同步与异步的 ``close``）。"""
    result = store.close()
    if inspect.isawaitable(result):
        asyncio.run(result)


@pytest.mark.unit
def test_connect_enables_wal_and_foreign_keys(tmp_path) -> None:
    conn = connect(str(tmp_path / "cp.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


@pytest.mark.unit
def test_connect_accepts_memory_database() -> None:
    conn = connect(":memory:")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    "open_store", [SQLiteRegistry, ProcessedKeyStore, TaskStore, AuditLog, Outbox]
)
def test_stores_open_the_database_in_wal_mode(tmp_path, open_store) -> None:
    path = str(tmp_path / "cp.db")
    store = open_store(path)
    try:
        probe = sqlite3.connect(path)
        try:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            probe.close()
    finally:
        _close(store)


@pytest.mark.unit
def test_open_read_does_not_block_writes_from_another_store(tmp_path) -> None:
    """注册表与去重键共用一个文件：读事务进行中，另一个连接仍可立即提交写入。"""
    path = str(tmp_path / "cp.db")
    store = ProcessedKeyStore(path)
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM processed_dedup_keys").fetchone()
        started = time.monotonic()
        asyncio.run(store.mark("key-a"))
        assert time.monotonic() - started < 1
        assert asyncio.run(store.seen("key-a")) is True
    finally:
        reader.rollback()
        reader.close()
        _close(store)
