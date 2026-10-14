"""控制平面 SQLite 连接的统一设置。

控制平面的各个存储（资源注册表、去重键等）在同一个数据库文件上各自打开连接，
全部经 :func:`connect` 打开：

* ``journal_mode=WAL``：读写互不阻塞，同一文件上的多个连接只在写入时排队。WAL 是
  数据库文件的持久属性，运行期间文件旁会有 ``-wal`` / ``-shm`` 文件，已提交的数据
  可能只在 ``-wal`` 中：不要只复制主库文件做备份（见
  ``docs/operations/backup-restore.md``），也不要把数据库放在网络文件系统上。
* ``foreign_keys=ON``：SQLite 默认不检查外键约束，且该设置只对当前连接生效。
* ``synchronous`` 保持默认的 ``FULL``：WAL 模式下 ``NORMAL`` 可能让已提交、已向客户端
  确认的事务在掉电后丢失，权威存储不接受。
* 等待锁的时长沿用 :func:`sqlite3.connect` 默认的 5 秒（``timeout``，即 ``busy_timeout``）。

资源注册表、Outbox 与任务存储经 :class:`Database` 访问连接：同一个连接上的操作由
它的锁串行化，可以把几个存储的写入放进同一个事务。

工作流运行时的存储（``forgemesh.workflowruntime.store.WorkflowStore``）不在这个文件里，
而是使用 :func:`workflow_db_path` 给出的同目录独立文件：它在后台频繁写入步骤状态，
放在同一个文件里会与资源注册表争用唯一的写锁（WAL 只让读写互不阻塞，写入仍然排队）。
两个文件各自启用 WAL，备份与恢复须同时覆盖两者。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

__all__ = ["Database", "connect", "workflow_db_path"]

_T = TypeVar("_T")


def connect(db_path: str) -> sqlite3.Connection:
    """打开控制平面使用的 SQLite 连接（允许跨线程使用）。

    Args:
        db_path: 数据库文件路径；``:memory:`` 时 WAL 不适用，仍为内存日志。

    Returns:
        已启用 WAL 与外键约束的连接。
    """
    # check_same_thread=False：asyncio.to_thread 会在不同线程执行 sqlite 调用。
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Database:
    """一个 SQLite 连接加一把可重入锁，供同一个数据库上的多个存储共用。

    sqlite3 的事务属于连接：多个线程交错使用同一个连接时，一方的 ``commit`` /
    ``rollback`` 会连带提交或丢弃另一方尚未完成的写入。共用一个 :class:`Database` 的
    存储只在持锁时使用连接，因此可以把几个存储的写入放进同一个事务，一起提交或回滚
    （ADR-008 的工作单元，例如资源写入与它的 Outbox 事件、审计记录）。

    Args:
        db_path: 数据库文件路径，默认 ``:memory:``（内存库只能经同一个实例共用）。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.path = db_path
        self.conn = connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def call(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """持锁执行 ``fn``，由 ``fn`` 自行提交；``fn`` 抛出异常时回滚它未提交的写入。

        Raises:
            RuntimeError: ``fn`` 正常返回，却留下了它开启的未提交事务（已回滚）。
        """
        with self.lock:
            already_open = self.conn.in_transaction
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                self._rollback()
                raise
            if not already_open and self.conn.in_transaction:
                self.conn.rollback()
                raise RuntimeError(f"{fn!r} left a transaction open; it has been rolled back")
            return result

    async def run(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """在工作线程中执行 :meth:`call`，不阻塞事件循环。"""
        return await asyncio.to_thread(self.call, fn, *args, **kwargs)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """持锁执行一组读写：开始即持有写锁（``BEGIN IMMEDIATE``），正常退出时提交，
        抛出异常时回滚。

        不能嵌套在另一个事务中：内层的提交会把外层尚未完成的写入一起提交。

        Raises:
            RuntimeError: 进入时连接上已有未提交的事务。
        """
        with self.lock:
            if self.conn.in_transaction:
                raise RuntimeError("a transaction is already open on this connection")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self._rollback()
                raise
            self.conn.commit()

    def close(self) -> None:
        """关闭连接（等待持锁的操作结束）。"""
        with self.lock:
            self.conn.close()

    def _rollback(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):  # 连接已关闭
            if self.conn.in_transaction:
                self.conn.rollback()


def workflow_db_path(db_path: str) -> str:
    """控制平面数据库 ``db_path`` 对应的工作流存储文件（同目录）。

    ``/data/forgemesh.db`` → ``/data/forgemesh.workflows.db``；没有扩展名时追加
    ``.workflows.db``；``:memory:`` 原样返回（每个连接各自是一个独立的内存库）。
    """
    if db_path == ":memory:":
        return db_path
    path = Path(db_path)
    if not path.suffix:
        return f"{db_path}.workflows.db"
    return str(path.with_suffix(".workflows.db"))
