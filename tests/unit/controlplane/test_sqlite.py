"""控制平面 SQLite 连接设置单元测试。

覆盖：

* ``connect`` 启用 WAL 与外键约束，``synchronous`` 保持 ``FULL``，等锁 5 秒；
* 控制平面的各个存储都以 WAL 模式打开数据库文件；
* 同一文件上一个连接的读事务不阻塞另一个存储写入；
* 工作流存储使用同目录的独立文件，控制平面库被写锁占用时照常写入；
* :class:`Database`：异常时回滚、拒绝遗留未提交的事务、事务整体提交或回滚，
  共用它的存储不会丢失彼此的写入。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import sqlite3
import time

import pytest
from forgemesh.controlplane.audit import AuditLog
from forgemesh.controlplane.outbox import IdempotentConsumer, Outbox, OutboxEvent
from forgemesh.controlplane.reconciler import ProcessedKeyStore
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.controlplane.sqlite import Database, connect, workflow_db_path
from forgemesh.controlplane.tasks import TaskStatus, TaskStore
from forgemesh.core.errors import NotFoundError
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflowruntime.store import WorkflowStore


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
    "open_store",
    [
        SQLiteRegistry,
        ProcessedKeyStore,
        TaskStore,
        AuditLog,
        Outbox,
        pytest.param(functools.partial(IdempotentConsumer, "wal"), id="IdempotentConsumer"),
    ],
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("db_path", "expected"),
    [
        ("/data/forgemesh.db", "/data/forgemesh.workflows.db"),
        ("relative/cp.sqlite3", "relative/cp.workflows.db"),
        ("/data.d/forgemesh", "/data.d/forgemesh.workflows.db"),
        (":memory:", ":memory:"),
    ],
)
def test_workflow_db_path_is_a_sibling_file(db_path: str, expected: str) -> None:
    assert workflow_db_path(db_path) == expected


@pytest.mark.unit
def test_workflow_store_writes_while_the_control_plane_database_is_locked(tmp_path) -> None:
    path = str(tmp_path / "cp.db")
    writer = connect(path)
    workflows = WorkflowStore(workflow_db_path(path))
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        asyncio.run(
            workflows.create_run(
                WorkflowDefinition(name="wf", steps=[Step(id="s1", uses="test.step")]), {}, "key-a"
            )
        )
        assert time.monotonic() - started < 1
        probe = sqlite3.connect(tmp_path / "cp.workflows.db")
        try:
            assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert probe.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] == 1
        finally:
            probe.close()
    finally:
        writer.rollback()
        writer.close()
        asyncio.run(workflows.close())


def _values(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY v")]


@pytest.mark.unit
def test_database_call_rolls_back_when_the_function_raises() -> None:
    db = Database()
    try:
        db.conn.execute("CREATE TABLE t (v TEXT)")

        def _fail() -> None:
            db.conn.execute("INSERT INTO t VALUES ('lost')")
            raise RuntimeError("boom")

        def _write() -> None:
            db.conn.execute("INSERT INTO t VALUES ('kept')")
            db.conn.commit()

        with pytest.raises(RuntimeError, match="boom"):
            db.call(_fail)
        assert not db.conn.in_transaction
        db.call(_write)
        assert _values(db.conn) == ["kept"]
    finally:
        db.close()


@pytest.mark.unit
def test_database_call_refuses_to_leave_a_transaction_open() -> None:
    db = Database()
    try:
        db.conn.execute("CREATE TABLE t (v TEXT)")
        with pytest.raises(RuntimeError, match="left a transaction open"):
            db.call(db.conn.execute, "INSERT INTO t VALUES ('uncommitted')")
        assert not db.conn.in_transaction
        assert _values(db.conn) == []
    finally:
        db.close()


@pytest.mark.unit
def test_database_transaction_commits_or_rolls_back_as_a_unit(tmp_path) -> None:
    path = str(tmp_path / "cp.db")
    db = Database(path)
    probe = sqlite3.connect(path, timeout=0)
    try:
        db.conn.execute("CREATE TABLE t (v TEXT)")
        with db.transaction() as conn:
            conn.execute("INSERT INTO t VALUES ('a')")
            assert _values(probe) == []
            # 事务开始即持有写锁：其他连接此时无法写入。
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                probe.execute("INSERT INTO t VALUES ('other')")
            probe.rollback()
            with pytest.raises(RuntimeError, match="already open"), db.transaction():
                pass
        assert _values(probe) == ["a"]

        with pytest.raises(RuntimeError, match="boom"), db.transaction() as conn:
            conn.execute("INSERT INTO t VALUES ('b')")
            raise RuntimeError("boom")
        assert not db.conn.in_transaction
        assert _values(probe) == ["a"]
    finally:
        probe.close()
        db.close()


@pytest.mark.unit
def test_stores_sharing_a_database_do_not_lose_each_others_writes(tmp_path) -> None:
    """任务存储与 Outbox 共用一个连接：一方的回滚不会连带丢弃另一方尚未提交的写入。"""

    async def scenario() -> None:
        database = Database(str(tmp_path / "cp.db"))
        tasks = TaskStore(database=database)
        outbox = Outbox(database=database)

        def put(event_id: str) -> None:
            with database.transaction():
                outbox.put(
                    OutboxEvent(
                        event_id=event_id, aggregate_type="T", aggregate_id="a", event_type="t"
                    )
                )

        try:
            for round_no in range(10):
                puts = [asyncio.to_thread(put, f"evt-{round_no}-{i}") for i in range(16)]
                misses = [
                    tasks.update_status(f"missing-{i}", TaskStatus.RUNNING) for i in range(16)
                ]
                results = await asyncio.gather(*puts, *misses, return_exceptions=True)
                assert results[:16] == [None] * 16
                assert all(isinstance(r, NotFoundError) for r in results[16:])
            assert outbox.get_stats() == {"pending": 160}
            await tasks.close()
            await outbox.close()
            assert database.call(lambda: database.conn.execute("SELECT 1").fetchone()[0]) == 1
        finally:
            database.close()

    asyncio.run(scenario())
