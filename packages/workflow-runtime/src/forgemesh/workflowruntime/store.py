"""工作流运行实例持久化（SQLite）。

对齐 development-plan 5.4 / ADR-011。运行实例固定定义版本与 Provider 版本，
进程重启后可恢复或进入明确人工处理状态。

设计要点
--------
* **幂等**：同一 ``idempotency_key`` 重复调用 ``create_run`` 返回已有 ``RunRef``，
  不创建新实例（``idempotency_key`` 在 DB 中为 UNIQUE 约束）。
* **版本固定**：``definition_version`` 在创建时由定义内容派生并固定，升级定义
  不影响既有实例（ADR-011）。
* **状态转移校验**：``update_run_status`` / ``update_step_status`` 调用
  :func:`validate_workflow_transition` / :func:`validate_step_transition`，
  非法转移抛 :class:`InvalidStateTransition`。
* **恢复**：``list_pending_runs`` 列出所有非终态运行，供进程重启后恢复。
* **线程安全**：SQLite 以 ``check_same_thread=False`` 打开，所有异步方法通过
  :func:`asyncio.to_thread` 在独立线程执行同步 sqlite3 调用。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
import zlib
from typing import Any

from forgemesh.core.errors import NotFoundError
from forgemesh.workflow.runtime import RunRef, WorkflowDefinition
from forgemesh.workflow.states import (
    StepStatus,
    WorkflowStatus,
    validate_step_transition,
    validate_workflow_transition,
)

#: 建表与建索引 DDL（幂等执行，对齐 development-plan 5.4）。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    definition_name TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    definition_json5 TEXT NOT NULL,
    inputs TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    timeout_seconds INTEGER
);
CREATE TABLE IF NOT EXISTS workflow_steps (
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    started_at REAL,
    finished_at REAL,
    result TEXT,
    PRIMARY KEY (run_id, step_id),
    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_idempotency ON workflow_runs(idempotency_key);
"""

#: 工作流终态集合（无出边），用于 ``list_pending_runs`` 过滤。
_WORKFLOW_TERMINAL_STATUSES: frozenset[str] = frozenset(
    s.value for s in (
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMEDOUT,
    )
)


def _derive_definition_version(defn: WorkflowDefinition) -> int:
    """根据工作流定义内容派生确定性的定义版本号。

    同一定义内容始终派生同一版本号；定义内容变化则版本号变化。
    使用 CRC32 取正整数（保证为 int 且确定）。
    """
    payload = defn.model_dump_json(by_alias=True, exclude_none=True)
    return zlib.crc32(payload.encode("utf-8")) & 0x7FFFFFFF


class WorkflowStore:
    """基于 SQLite 的工作流运行实例持久化存储。

    Args:
        db_path: SQLite 数据库文件路径，默认 ``:memory:``（内存库，单测友好）。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        # check_same_thread=False：asyncio.to_thread 会在不同线程执行 sqlite 调用。
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
    def _now() -> float:
        """当前 Unix 时间戳（秒）。"""
        return time.time()

    # ------------------------------------------------------------------ #
    # 同步实现（由 async 方法通过 asyncio.to_thread 调用）
    # ------------------------------------------------------------------ #

    def _create_run_sync(
        self,
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        """同步创建运行实例（幂等）。"""
        # 幂等检查：同一 idempotency_key 已存在则返回已有实例。
        existing = self._conn.execute(
            "SELECT run_id, definition_version FROM workflow_runs "
            "WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return RunRef(
                run_id=existing["run_id"],
                definition_version=existing["definition_version"],
            )

        run_id = f"run-{uuid.uuid4().hex}"
        definition_version = _derive_definition_version(defn)
        definition_json5 = defn.model_dump_json(by_alias=True, exclude_none=True)
        inputs_json = json.dumps(inputs, ensure_ascii=False, sort_keys=True)
        now = self._now()

        self._conn.execute(
            """
            INSERT INTO workflow_runs (
                run_id, idempotency_key, definition_name, definition_version,
                definition_json5, inputs, status, created_at, updated_at,
                timeout_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                idempotency_key,
                defn.name,
                definition_version,
                definition_json5,
                inputs_json,
                WorkflowStatus.PENDING.value,
                now,
                now,
                defn.timeout_seconds,
            ),
        )

        # 为定义中的每个步骤初始化 Pending 行。
        for step in defn.steps:
            self._conn.execute(
                """
                INSERT INTO workflow_steps (run_id, step_id, status)
                VALUES (?, ?, ?)
                """,
                (run_id, step.id, StepStatus.PENDING.value),
            )

        self._conn.commit()
        return RunRef(run_id=run_id, definition_version=definition_version)

    def _get_run_sync(self, run_id: str) -> dict[str, Any]:
        """同步查询运行实例（含步骤状态）。"""
        row = self._conn.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"run not found: {run_id}")

        steps = self._conn.execute(
            "SELECT * FROM workflow_steps WHERE run_id = ? ORDER BY step_id",
            (run_id,),
        ).fetchall()

        return {
            "run_id": row["run_id"],
            "idempotency_key": row["idempotency_key"],
            "definition_name": row["definition_name"],
            "definition_version": row["definition_version"],
            "definition_json5": row["definition_json5"],
            "inputs": json.loads(row["inputs"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "timeout_seconds": row["timeout_seconds"],
            "steps": [
                {
                    "step_id": s["step_id"],
                    "status": s["status"],
                    "attempt_count": s["attempt_count"],
                    "started_at": s["started_at"],
                    "finished_at": s["finished_at"],
                    "result": json.loads(s["result"]) if s["result"] else None,
                }
                for s in steps
            ],
        }

    def _update_run_status_sync(self, run_id: str, status: str) -> None:
        """同步更新运行状态（校验转移合法性）。"""
        row = self._conn.execute(
            "SELECT status FROM workflow_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"run not found: {run_id}")

        cur_status = row["status"]
        # 校验转移合法性（非法转移抛 InvalidStateTransition）。
        validate_workflow_transition(cur_status, status)

        now = self._now()
        self._conn.execute(
            "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, now, run_id),
        )
        self._conn.commit()

    def _update_step_status_sync(
        self,
        run_id: str,
        step_id: str,
        status: str,
        result: Any | None = None,
    ) -> None:
        """同步更新步骤状态（校验转移合法性）。"""
        row = self._conn.execute(
            "SELECT status, started_at FROM workflow_steps "
            "WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"step not found: run_id={run_id}, step_id={step_id}"
            )

        cur_status = row["status"]
        # 校验转移合法性。
        validate_step_transition(cur_status, status)

        now = self._now()
        started_at = row["started_at"]
        if status == StepStatus.RUNNING.value and started_at is None:
            started_at = now
        finished_at = now if status in (
            StepStatus.SUCCEEDED.value,
            StepStatus.FAILED.value,
            StepStatus.SKIPPED.value,
            StepStatus.CANCELLED.value,
            StepStatus.COMPENSATED.value,
            StepStatus.COMPENSATION_FAILED.value,
        ) else None

        result_json = json.dumps(result, ensure_ascii=False) if result is not None else None

        self._conn.execute(
            """
            UPDATE workflow_steps
            SET status = ?, started_at = ?, finished_at = ?, result = ?
            WHERE run_id = ? AND step_id = ?
            """,
            (status, started_at, finished_at, result_json, run_id, step_id),
        )
        self._conn.commit()

    def _list_pending_runs_sync(self) -> list[dict[str, Any]]:
        """同步列出非终态运行（用于恢复）。"""
        rows = self._conn.execute(
            "SELECT * FROM workflow_runs "
            "WHERE status NOT IN (?, ?, ?, ?) "
            "ORDER BY created_at",
            tuple(_WORKFLOW_TERMINAL_STATUSES),
        ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "idempotency_key": r["idempotency_key"],
                "definition_name": r["definition_name"],
                "definition_version": r["definition_version"],
                "status": r["status"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "timeout_seconds": r["timeout_seconds"],
            }
            for r in rows
        ]

    def _get_step_statuses_sync(self, run_id: str) -> dict[str, StepStatus]:
        """同步获取所有步骤状态。"""
        rows = self._conn.execute(
            "SELECT step_id, status FROM workflow_steps WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        if not rows:
            # 运行实例可能存在但无步骤行（理论上 create_run 会初始化），
            # 仅当 run_id 完全不存在时报 NotFoundError。
            run_row = self._conn.execute(
                "SELECT 1 FROM workflow_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise NotFoundError(f"run not found: {run_id}")
        return {r["step_id"]: StepStatus(r["status"]) for r in rows}

    def _close_sync(self) -> None:
        """同步关闭连接。"""
        self._conn.close()

    # ------------------------------------------------------------------ #
    # 公开异步 API
    # ------------------------------------------------------------------ #

    async def create_run(
        self,
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        """创建运行实例（幂等）。

        同一 ``idempotency_key`` 重复调用返回已有 ``RunRef``，不创建新实例。
        ``definition_version`` 在创建时由定义内容派生并固定。

        Args:
            defn: 工作流定义。
            inputs: 运行时输入。
            idempotency_key: 幂等键。

        Returns:
            指向新建或既有运行实例的 :class:`RunRef`。
        """
        return await asyncio.to_thread(
            self._create_run_sync, defn, inputs, idempotency_key
        )

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """查询运行实例（含步骤状态）。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        return await asyncio.to_thread(self._get_run_sync, run_id)

    async def update_run_status(self, run_id: str, status: str) -> None:
        """更新运行状态（校验转移合法性）。

        Raises:
            NotFoundError: 运行实例不存在。
            InvalidStateTransition: 状态转移不合法。
        """
        await asyncio.to_thread(self._update_run_status_sync, run_id, status)

    async def update_step_status(
        self,
        run_id: str,
        step_id: str,
        status: str,
        result: Any | None = None,
    ) -> None:
        """更新步骤状态（校验转移合法性）。

        Raises:
            NotFoundError: 步骤不存在。
            InvalidStateTransition: 状态转移不合法。
        """
        await asyncio.to_thread(
            self._update_step_status_sync, run_id, step_id, status, result
        )

    async def list_pending_runs(self) -> list[dict[str, Any]]:
        """列出非终态运行（用于进程重启后恢复）。"""
        return await asyncio.to_thread(self._list_pending_runs_sync)

    async def get_step_statuses(self, run_id: str) -> dict[str, StepStatus]:
        """获取所有步骤状态。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        return await asyncio.to_thread(self._get_step_statuses_sync, run_id)

    async def close(self) -> None:
        """关闭数据库连接。"""
        await asyncio.to_thread(self._close_sync)


__all__ = [
    "WorkflowStore",
]
