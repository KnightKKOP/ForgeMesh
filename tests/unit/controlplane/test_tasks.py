"""``forgemesh.controlplane.tasks`` 单元测试。

覆盖 TaskStore 契约实现的核心语义：

* Task 创建 / 查询 / 更新状态；
* 幂等：同 idempotency_key 重复创建返回已有任务；
* list_pending（全部 / 按 agent_id 过滤）；
* list_by_idempotency_key；
* TaskDeduplicator should_execute 首次 / 重复、mark_complete；
* 不存在 RUN_SHELL 类型（验证枚举不含，ADR-009）。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from forgemesh.controlplane.tasks import (
    Task,
    TaskDeduplicator,
    TaskStatus,
    TaskStore,
    TaskType,
)
from forgemesh.core.errors import NotFoundError
from pydantic import ValidationError


def _make_task(
    task_id: str = "task-1",
    execution_id: str = "exec-1",
    idempotency_key: str = "idem-1",
    task_type: TaskType = TaskType.PROBE_GPU,
    agent_id: str | None = None,
) -> Task:
    """构造一个可复用的最小 Task。"""
    return Task(
        task_id=task_id,
        execution_id=execution_id,
        idempotency_key=idempotency_key,
        task_type=task_type,
        input=b'{"gpu": "all"}',
        deadline="2026-12-31T23:59:59Z",
        correlation_id="corr-1",
        agent_id=agent_id,
    )


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# TaskType 枚举（ADR-009：不存在 RUN_SHELL / EXEC）
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_task_type_has_exactly_six_controlled_types() -> None:
    """TaskType 仅含 6 类受控任务。"""
    members = {m.name for m in TaskType}
    assert members == {
        "PROBE_GPU",
        "VALIDATE_DRIVER",
        "PREPARE_RUNTIME",
        "DEPLOY_INFERENCE_SERVICE",
        "INSPECT_SERVICE",
        "RELEASE_ALLOCATION",
    }


@pytest.mark.unit
def test_task_type_does_not_contain_run_shell() -> None:
    """ADR-009：不存在 RUN_SHELL / EXEC 任意命令类型。"""
    members = {m.name for m in TaskType}
    assert "RUN_SHELL" not in members
    assert "EXEC" not in members
    assert "RUN_SHELL" not in dir(TaskType)
    assert "EXEC" not in dir(TaskType)


# ---------------------------------------------------------------------- #
# TaskStatus 枚举
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_task_status_has_six_states() -> None:
    """TaskStatus 含 6 态。"""
    members = {m.name for m in TaskStatus}
    assert members == {
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TIMED_OUT",
    }


# ---------------------------------------------------------------------- #
# Task 模型
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_task_model_defaults() -> None:
    """Task 默认状态为 PENDING，input 为空 bytes。"""
    task = Task(
        task_id="t",
        execution_id="e",
        idempotency_key="k",
        task_type=TaskType.PROBE_GPU,
    )
    assert task.status is TaskStatus.PENDING
    assert task.input == b""
    assert task.agent_id is None
    assert task.result is None


@pytest.mark.unit
def test_task_model_is_frozen() -> None:
    """Task 为不可变值对象。"""
    task = _make_task()
    with pytest.raises(ValidationError):
        task.status = TaskStatus.RUNNING  # type: ignore[misc]


# ---------------------------------------------------------------------- #
# TaskStore: create / get
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_create_and_get() -> None:
    """创建任务后可按 task_id 查询往返。"""
    store = TaskStore()
    task = _make_task()

    created = _run(store.create(task))
    assert created.task_id == "task-1"
    assert created.status is TaskStatus.PENDING
    assert created.created_at > 0
    assert created.updated_at > 0

    got = _run(store.get("task-1"))
    assert got.task_id == "task-1"
    assert got.task_type is TaskType.PROBE_GPU
    assert got.input == b'{"gpu": "all"}'
    assert got.deadline == "2026-12-31T23:59:59Z"
    assert got.correlation_id == "corr-1"


@pytest.mark.unit
def test_get_not_found() -> None:
    """查询不存在的任务抛 NotFoundError。"""
    store = TaskStore()
    with pytest.raises(NotFoundError):
        _run(store.get("nonexistent"))


# ---------------------------------------------------------------------- #
# TaskStore: 幂等
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_create_idempotent_returns_existing() -> None:
    """同 idempotency_key 重复创建返回已有任务，不产生新记录。"""
    store = TaskStore()
    task_a = _make_task(task_id="task-a", idempotency_key="shared-key")
    task_b = _make_task(
        task_id="task-b",
        idempotency_key="shared-key",
        task_type=TaskType.VALIDATE_DRIVER,
    )

    first = _run(store.create(task_a))
    assert first.task_id == "task-a"

    # 同 idempotency_key 再次创建 → 返回已有任务（task-a），而非 task-b
    second = _run(store.create(task_b))
    assert second.task_id == "task-a"
    assert second.task_type is TaskType.PROBE_GPU

    # task-b 未被写入
    with pytest.raises(NotFoundError):
        _run(store.get("task-b"))


@pytest.mark.unit
def test_list_by_idempotency_key() -> None:
    """按幂等键查询：存在返回 Task，不存在返回 None。"""
    store = TaskStore()
    _run(store.create(_make_task(idempotency_key="key-1")))

    found = _run(store.list_by_idempotency_key("key-1"))
    assert found is not None
    assert found.idempotency_key == "key-1"

    missing = _run(store.list_by_idempotency_key("key-missing"))
    assert missing is None


# ---------------------------------------------------------------------- #
# TaskStore: update_status
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_update_status() -> None:
    """更新任务状态与结果载荷。"""
    store = TaskStore()
    _run(store.create(_make_task()))

    _run(store.update_status("task-1", TaskStatus.RUNNING))
    got = _run(store.get("task-1"))
    assert got.status is TaskStatus.RUNNING

    _run(store.update_status("task-1", TaskStatus.SUCCEEDED, result=b'{"ok": true}'))
    got = _run(store.get("task-1"))
    assert got.status is TaskStatus.SUCCEEDED
    assert got.result == b'{"ok": true}'


@pytest.mark.unit
def test_update_status_not_found() -> None:
    """更新不存在的任务抛 NotFoundError。"""
    store = TaskStore()
    with pytest.raises(NotFoundError):
        _run(store.update_status("nope", TaskStatus.RUNNING))


# ---------------------------------------------------------------------- #
# TaskStore: list_pending
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_pending_all() -> None:
    """list_pending 返回所有非终态任务。"""
    store = TaskStore()
    _run(store.create(_make_task(task_id="t1", idempotency_key="k1")))
    _run(store.create(_make_task(task_id="t2", idempotency_key="k2")))
    _run(store.create(_make_task(task_id="t3", idempotency_key="k3")))

    # 将 t3 标记为终态
    _run(store.update_status("t3", TaskStatus.SUCCEEDED))

    pending = _run(store.list_pending())
    pending_ids = {t.task_id for t in pending}
    assert pending_ids == {"t1", "t2"}


@pytest.mark.unit
def test_list_pending_by_agent() -> None:
    """list_pending(agent_id) 仅返回分配给该 agent 的非终态任务。"""
    store = TaskStore()
    _run(store.create(_make_task(task_id="t1", idempotency_key="k1", agent_id="agent-a")))
    _run(store.create(_make_task(task_id="t2", idempotency_key="k2", agent_id="agent-b")))
    _run(store.create(_make_task(task_id="t3", idempotency_key="k3", agent_id="agent-a")))

    # agent-a 的 t2 进入终态不影响
    _run(store.update_status("t2", TaskStatus.FAILED))

    pending_a = _run(store.list_pending(agent_id="agent-a"))
    assert {t.task_id for t in pending_a} == {"t1", "t3"}

    pending_b = _run(store.list_pending(agent_id="agent-b"))
    assert pending_b == []


@pytest.mark.unit
def test_list_pending_excludes_all_terminal_states() -> None:
    """所有终态任务均被 list_pending 排除。"""
    store = TaskStore()
    for i, status in enumerate(
        [TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT],
        start=1,
    ):
        tid = f"t{i}"
        _run(store.create(_make_task(task_id=tid, idempotency_key=f"k{i}")))
        _run(store.update_status(tid, status))

    # 再加一个 PENDING 和一个 RUNNING
    _run(store.create(_make_task(task_id="t9", idempotency_key="k9")))
    _run(store.create(_make_task(task_id="t10", idempotency_key="k10")))
    _run(store.update_status("t10", TaskStatus.RUNNING))

    pending = _run(store.list_pending())
    assert {t.task_id for t in pending} == {"t9", "t10"}


# ---------------------------------------------------------------------- #
# TaskStore: close
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_close() -> None:
    """close 关闭连接，后续操作抛异常。"""
    store = TaskStore()
    _run(store.close())
    with pytest.raises(sqlite3.ProgrammingError):
        _run(store.get("task-1"))


# ---------------------------------------------------------------------- #
# TaskDeduplicator
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_deduplicator_should_execute_first_time() -> None:
    """首次出现的幂等键返回 True。"""
    dedup = TaskDeduplicator()
    assert dedup.should_execute("task-1", "key-1") is True


@pytest.mark.unit
def test_deduplicator_should_execute_duplicate() -> None:
    """重复幂等键返回 False。"""
    dedup = TaskDeduplicator()
    assert dedup.should_execute("task-1", "key-1") is True
    assert dedup.should_execute("task-2", "key-1") is False


@pytest.mark.unit
def test_deduplicator_different_keys() -> None:
    """不同幂等键各自独立判重。"""
    dedup = TaskDeduplicator()
    assert dedup.should_execute("task-1", "key-a") is True
    assert dedup.should_execute("task-2", "key-b") is True
    assert dedup.should_execute("task-3", "key-a") is False
    assert dedup.should_execute("task-4", "key-b") is False


@pytest.mark.unit
def test_deduplicator_mark_complete() -> None:
    """mark_complete 标记完成，is_completed 可查询。"""
    dedup = TaskDeduplicator()
    dedup.should_execute("task-1", "key-1")
    assert dedup.is_completed("task-1") is False

    dedup.mark_complete("task-1")
    assert dedup.is_completed("task-1") is True
    assert dedup.is_completed("task-2") is False
