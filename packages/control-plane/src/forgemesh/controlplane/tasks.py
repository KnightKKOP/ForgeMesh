"""gRPC 任务协议：任务模型、幂等持久化与去重。

对齐 ``api/protobuf/agent.proto`` 与 development-plan 5.9 / ADR-009。

设计要点
--------
* **受控任务类型**：:class:`TaskType` 仅含 6 类有类型任务，**不存在 RUN_SHELL /
  EXEC 等任意命令类型**（ADR-009）。
* **幂等**：:class:`TaskStore.create` 以 ``idempotency_key`` 为唯一约束，同一键
  重复创建返回已有任务，不产生新记录（断线重连去重）。
* **SQLite 持久化**：与 :class:`WorkflowStore` 风格一致，所有异步方法通过
  :func:`asyncio.to_thread` 包装同步 sqlite3 调用，``check_same_thread=False``。
* **去重器**：:class:`TaskDeduplicator` 提供进程内轻量去重（与 SQLite 幂等互补，
  用于下发前的快速判重）。

兼容 Python 3.9+（运行环境 3.12，使用 :class:`enum.StrEnum`）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from enum import StrEnum

from forgemesh.core.errors import NotFoundError
from pydantic import BaseModel, ConfigDict

from .sqlite import connect

# ---------------------------------------------------------------------- #
# 枚举
# ---------------------------------------------------------------------- #


class TaskType(StrEnum):
    """任务类型枚举（对齐 ``agent.proto`` TaskType，6 类受控任务）。

    .. note::
        **不存在 RUN_SHELL / EXEC 任意命令类型**（ADR-009）。
    """

    PROBE_GPU = "PROBE_GPU"
    VALIDATE_DRIVER = "VALIDATE_DRIVER"
    PREPARE_RUNTIME = "PREPARE_RUNTIME"
    DEPLOY_INFERENCE_SERVICE = "DEPLOY_INFERENCE_SERVICE"
    INSPECT_SERVICE = "INSPECT_SERVICE"
    RELEASE_ALLOCATION = "RELEASE_ALLOCATION"


class TaskStatus(StrEnum):
    """任务执行状态枚举（对齐 ``agent.proto`` TaskResult.State）。

    终态：``SUCCEEDED`` / ``FAILED`` / ``CANCELLED`` / ``TIMED_OUT``。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


#: 任务终态集合（无出边），用于 ``list_pending`` 过滤。
_TASK_TERMINAL_STATUSES: frozenset[str] = frozenset(
    s.value for s in (
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    )
)


# ---------------------------------------------------------------------- #
# 任务模型
# ---------------------------------------------------------------------- #


class Task(BaseModel):
    """任务下发单元（对齐 ``agent.proto`` Task）。

    不可变值对象：状态更新通过 :meth:`pydantic.BaseModel.model_copy` 派生新实例，
    不就地修改。

    Attributes:
        task_id: 全局任务标识（主键）。
        execution_id: 一次执行尝试标识。
        idempotency_key: 幂等键（重连去重，唯一约束）。
        task_type: 任务类型（6 类受控任务之一）。
        input: 由 input_schema 校验的类型化载荷（protobuf bytes）。
        deadline: RFC 3339 截止时间字符串；``None`` 表示无超时。
        correlation_id: 全链路关联 ID（INV-4）。
        agent_id: 任务分配的目标 Agent；``None`` 表示尚未分配。
        status: 任务执行状态。
        result: 执行结果载荷（终态时写入）。
        created_at: 创建时间戳（Unix 秒）。
        updated_at: 最近更新时间戳（Unix 秒）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    execution_id: str
    idempotency_key: str
    task_type: TaskType
    input: bytes = b""
    deadline: str | None = None
    correlation_id: str | None = None
    agent_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: bytes | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


# ---------------------------------------------------------------------- #
# TaskStore —— SQLite 持久化
# ---------------------------------------------------------------------- #


#: 建表与建索引 DDL（幂等执行）。
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    task_type TEXT NOT NULL,
    input BLOB NOT NULL DEFAULT x'',
    deadline TEXT,
    correlation_id TEXT,
    agent_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    result BLOB,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_status ON tasks(agent_id, status);
"""


class TaskStore:
    """基于 SQLite 的任务持久化存储。

    与 :class:`forgemesh.workflowruntime.WorkflowStore` 风格一致：所有异步方法
    通过 :func:`asyncio.to_thread` 在独立线程执行同步 sqlite3 调用。

    Args:
        db_path: SQLite 数据库文件路径，默认 ``:memory:``（内存库，单测友好）。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn = connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _init_schema(self) -> None:
        """创建表与索引（幂等）。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    @staticmethod
    def _now() -> float:
        """当前 Unix 时间戳（秒）。"""
        return time.time()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        """将数据库行反序列化为 :class:`Task`。"""
        return Task(
            task_id=row["task_id"],
            execution_id=row["execution_id"],
            idempotency_key=row["idempotency_key"],
            task_type=TaskType(row["task_type"]),
            input=bytes(row["input"]) if row["input"] is not None else b"",
            deadline=row["deadline"],
            correlation_id=row["correlation_id"],
            agent_id=row["agent_id"],
            status=TaskStatus(row["status"]),
            result=bytes(row["result"]) if row["result"] is not None else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------ #
    # 同步实现（由 async 方法通过 asyncio.to_thread 调用）
    # ------------------------------------------------------------------ #

    def _create_sync(self, task: Task) -> Task:
        """同步创建任务（幂等：同 idempotency_key 返回已有任务）。"""
        # 幂等检查：同一 idempotency_key 已存在则返回已有任务。
        existing = self._conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?",
            (task.idempotency_key,),
        ).fetchone()
        if existing is not None:
            return self._row_to_task(existing)

        now = self._now()
        stored = task.model_copy(
            update={
                "status": task.status if task.status else TaskStatus.PENDING,
                "created_at": now if task.created_at == 0.0 else task.created_at,
                "updated_at": now if task.updated_at == 0.0 else task.updated_at,
            },
        )

        self._conn.execute(
            """
            INSERT INTO tasks (
                task_id, execution_id, idempotency_key, task_type,
                input, deadline, correlation_id, agent_id, status,
                result, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.task_id,
                stored.execution_id,
                stored.idempotency_key,
                stored.task_type.value,
                stored.input,
                stored.deadline,
                stored.correlation_id,
                stored.agent_id,
                stored.status.value,
                stored.result,
                stored.created_at,
                stored.updated_at,
            ),
        )
        self._conn.commit()
        return stored

    def _get_sync(self, task_id: str) -> Task:
        """同步查询单个任务。"""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"task not found: {task_id}")
        return self._row_to_task(row)

    def _update_status_sync(
        self,
        task_id: str,
        status: TaskStatus,
        result: bytes | None,
    ) -> None:
        """同步更新任务状态与结果。"""
        now = self._now()
        cur = self._conn.execute(
            "UPDATE tasks SET status = ?, result = ?, updated_at = ? "
            "WHERE task_id = ?",
            (status.value, result, now, task_id),
        )
        if cur.rowcount == 0:
            self._conn.rollback()
            raise NotFoundError(f"task not found: {task_id}")
        self._conn.commit()

    def _list_pending_sync(self, agent_id: str | None) -> list[Task]:
        """同步列出待执行（非终态）任务。"""
        if agent_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE agent_id = ? AND status NOT IN "
                f"({', '.join('?' for _ in _TASK_TERMINAL_STATUSES)}) "
                "ORDER BY created_at",
                (agent_id, *_TASK_TERMINAL_STATUSES),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status NOT IN "
                f"({', '.join('?' for _ in _TASK_TERMINAL_STATUSES)}) "
                "ORDER BY created_at",
                tuple(_TASK_TERMINAL_STATUSES),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _list_by_idempotency_key_sync(self, idempotency_key: str) -> Task | None:
        """同步按幂等键查询任务。"""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def _close_sync(self) -> None:
        """同步关闭连接。"""
        self._conn.close()

    # ------------------------------------------------------------------ #
    # 公开异步 API
    # ------------------------------------------------------------------ #

    async def create(self, task: Task) -> Task:
        """创建任务（幂等）。

        同一 ``idempotency_key`` 重复调用返回已有任务，不创建新记录。
        若新建任务的 ``created_at`` / ``updated_at`` 为 0，则以当前时间戳填充。

        Args:
            task: 待创建的任务。

        Returns:
            写入后的任务（含时间戳）；或同幂等键的既有任务。
        """
        return await asyncio.to_thread(self._create_sync, task)

    async def get(self, task_id: str) -> Task:
        """查询单个任务。

        Raises:
            NotFoundError: 任务不存在。
        """
        return await asyncio.to_thread(self._get_sync, task_id)

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: bytes | None = None,
    ) -> None:
        """更新任务状态与可选的结果载荷。

        Raises:
            NotFoundError: 任务不存在。
        """
        await asyncio.to_thread(self._update_status_sync, task_id, status, result)

    async def list_pending(self, agent_id: str | None = None) -> list[Task]:
        """列出待执行（非终态）任务。

        Args:
            agent_id: 可选的目标 Agent 过滤；``None`` 返回所有待执行任务。

        Returns:
            按 ``created_at`` 升序排列的待执行任务列表。
        """
        return await asyncio.to_thread(self._list_pending_sync, agent_id)

    async def list_by_idempotency_key(self, idempotency_key: str) -> Task | None:
        """按幂等键查询任务，不存在返回 ``None``。"""
        return await asyncio.to_thread(
            self._list_by_idempotency_key_sync, idempotency_key
        )

    async def close(self) -> None:
        """关闭数据库连接。"""
        await asyncio.to_thread(self._close_sync)


# ---------------------------------------------------------------------- #
# TaskDeduplicator —— 进程内轻量去重
# ---------------------------------------------------------------------- #


class TaskDeduplicator:
    """进程内任务去重器。

    与 :class:`TaskStore` 的 SQLite 幂等互补：用于任务下发前的快速判重，
    避免重复下发同一 ``idempotency_key`` 的任务。

    线程安全性：本类为单线程设计（控制平面事件循环内调用）；
    跨线程场景请使用 :class:`TaskStore` 的 SQLite 幂等约束。
    """

    def __init__(self) -> None:
        #: 已提交的幂等键 → task_id 映射。
        self._seen: dict[str, str] = {}
        #: 已完成的 task_id 集合。
        self._completed: set[str] = set()

    def should_execute(self, task_id: str, idempotency_key: str) -> bool:
        """判断任务是否应执行。

        首次出现的 ``idempotency_key`` 返回 ``True`` 并记录；
        重复 ``idempotency_key`` 返回 ``False``。

        Args:
            task_id: 任务标识。
            idempotency_key: 幂等键（去重比对依据）。

        Returns:
            ``True`` 表示应执行（首次），``False`` 表示重复应跳过。
        """
        if idempotency_key in self._seen:
            return False
        self._seen[idempotency_key] = task_id
        return True

    def mark_complete(self, task_id: str) -> None:
        """标记任务完成。

        Args:
            task_id: 已完成的任务标识。
        """
        self._completed.add(task_id)

    def is_completed(self, task_id: str) -> bool:
        """查询任务是否已完成。"""
        return task_id in self._completed


__all__ = [
    "Task",
    "TaskDeduplicator",
    "TaskStatus",
    "TaskStore",
    "TaskType",
]
