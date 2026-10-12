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

工作流运行时的存储（``forgemesh.workflowruntime.store.WorkflowStore``）不在这个文件里，
而是使用 :func:`workflow_db_path` 给出的同目录独立文件：它在后台频繁写入步骤状态，
放在同一个文件里会与资源注册表争用唯一的写锁（WAL 只让读写互不阻塞，写入仍然排队）。
两个文件各自启用 WAL，备份与恢复须同时覆盖两者。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["connect", "workflow_db_path"]


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
