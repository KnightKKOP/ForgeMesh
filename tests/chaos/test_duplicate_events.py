"""混沌测试：重复事件 / 结果丢失（INV-2/INV-5）。

验证外部成功但回报丢失时不盲目重试不可幂等操作：

* ``TaskStore`` 中任务已 ``SUCCEEDED``，重复上报不回退状态、不重复调度；
* ``TaskDeduplicator`` 对已完成 / 重复任务返回 ``False``（不重复执行）；
* ``RecoveryManager`` 查询真实状态后不盲目重试（succeeded → report_success）；
* ``IdempotencyGuard`` 对重复 ``task_id`` 返回 ``False``。

使用 ``AsyncMock`` 模拟控制平面远端，不需要真实硬件。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from forgemesh.controlplane.tasks import (
    Task,
    TaskDeduplicator,
    TaskStatus,
    TaskStore,
    TaskType,
)
from forgemesh.nodeagent.recover import (
    IdempotencyGuard,
    RecoveryManager,
    RunningExecution,
)

pytestmark = pytest.mark.unit


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_task(task_id: str = "task-1", idem_key: str = "idem-1") -> Task:
    """构造最小任务（PROBE_GPU 类型）。"""
    return Task(
        task_id=task_id,
        execution_id=f"exec-{task_id}",
        idempotency_key=idem_key,
        task_type=TaskType.PROBE_GPU,
    )


# ---------------------------------------------------------------------- #
# TaskStore：重复上报不回退状态
# ---------------------------------------------------------------------- #


def test_succeeded_task_not_re_executed(tmp_path: pytest.TempPathFactory) -> None:
    """已 SUCCEEDED 的任务重复上报结果，状态不回退。"""
    store = TaskStore(str(tmp_path / "tasks.db"))
    _run(store.create(_make_task("t1", "k1")))
    _run(store.update_status("t1", TaskStatus.SUCCEEDED, b"result"))

    # 模拟回报丢失后重传：重复上报成功
    _run(store.update_status("t1", TaskStatus.SUCCEEDED, b"result-dup"))

    fetched = _run(store.get("t1"))
    assert fetched.status is TaskStatus.SUCCEEDED
    _run(store.close())


def test_duplicate_create_returns_existing(tmp_path: pytest.TempPathFactory) -> None:
    """同 idempotency_key 重复创建返回已有任务，不产生新记录。"""
    store = TaskStore(str(tmp_path / "tasks.db"))
    created = _run(store.create(_make_task("t1", "k1")))
    # 不同 task_id 但同幂等键 → 返回已有任务
    again = _run(store.create(_make_task("t2", "k1")))

    assert again.task_id == created.task_id
    _run(store.close())


def test_succeeded_task_excluded_from_pending(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """已 SUCCEEDED 的任务不在 list_pending 中（不重复调度）。"""
    store = TaskStore(str(tmp_path / "tasks.db"))
    _run(store.create(_make_task("t1", "k1")))
    _run(store.update_status("t1", TaskStatus.SUCCEEDED))

    assert _run(store.list_pending()) == []
    _run(store.close())


# ---------------------------------------------------------------------- #
# TaskDeduplicator：已完成 / 重复任务不执行
# ---------------------------------------------------------------------- #


def test_deduplicator_rejects_duplicate_key() -> None:
    """重复 idempotency_key 返回 False。"""
    dedup = TaskDeduplicator()
    assert dedup.should_execute("t1", "k1") is True
    assert dedup.should_execute("t2", "k1") is False


def test_deduplicator_completed_task_not_re_executed() -> None:
    """已标记完成的任务不再执行。"""
    dedup = TaskDeduplicator()
    dedup.should_execute("t1", "k1")
    dedup.mark_complete("t1")
    assert dedup.is_completed("t1") is True
    assert dedup.should_execute("t1", "k1") is False


# ---------------------------------------------------------------------- #
# RecoveryManager：查询真实状态后不盲目重试
# ---------------------------------------------------------------------- #


def test_recovery_no_blind_retry_on_succeeded() -> None:
    """远端已 succeeded → report_success，不盲目重试（resume）。"""
    server = AsyncMock()
    server.get_execution_state = AsyncMock(return_value="succeeded")
    mgr = RecoveryManager()

    actions = _run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="t1", execution_id="e1")],
        )
    )

    assert len(actions) == 1
    assert actions[0].action == "report_success"
    assert actions[0].action != "resume"


def test_recovery_no_blind_retry_on_failed() -> None:
    """远端已 failed → report_failure，不盲目重试。"""
    server = AsyncMock()
    server.get_execution_state = AsyncMock(return_value="failed")
    mgr = RecoveryManager()

    actions = _run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="t1", execution_id="e1")],
        )
    )

    assert actions[0].action == "report_failure"
    assert actions[0].action != "resume"


def test_recovery_no_blind_retry_on_cancelled() -> None:
    """远端已 cancelled → cancel，不盲目重试。"""
    server = AsyncMock()
    server.get_execution_state = AsyncMock(return_value="cancelled")
    mgr = RecoveryManager()

    actions = _run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="t1", execution_id="e1")],
        )
    )

    assert actions[0].action == "cancel"
    assert actions[0].action != "resume"


def test_recovery_resume_only_when_running_or_unknown() -> None:
    """仅远端 running 或未知时才 resume（乐观恢复）。"""
    mgr = RecoveryManager()
    cases = [("running", True), (None, True), ("succeeded", False)]

    for state, expected_resume in cases:
        server = AsyncMock()
        server.get_execution_state = AsyncMock(return_value=state)
        actions = _run(
            mgr.recover(
                server,
                agent_id="agent-1",
                running_executions=[RunningExecution(task_id="t1", execution_id="e1")],
            )
        )
        is_resume = actions[0].action == "resume"
        assert is_resume is expected_resume, f"state={state!r}"


# ---------------------------------------------------------------------- #
# IdempotencyGuard：重复 task_id 返回 False
# ---------------------------------------------------------------------- #


def test_idempotency_guard_rejects_duplicate() -> None:
    """重复 task_id + idempotency_key 返回 False。"""
    guard = IdempotencyGuard()
    assert guard.check_and_mark("t1", "k1") is True
    assert guard.check_and_mark("t1", "k1") is False


def test_idempotency_guard_different_keys_independent() -> None:
    """不同组合键互不影响。"""
    guard = IdempotencyGuard()
    assert guard.check_and_mark("t1", "ka") is True
    assert guard.check_and_mark("t1", "kb") is True
    assert guard.check_and_mark("t2", "ka") is True
    assert guard.check_and_mark("t1", "ka") is False


# ---------------------------------------------------------------------- #
# 综合场景：结果丢失后不盲目重试
# ---------------------------------------------------------------------- #


def test_result_lost_no_blind_retry(tmp_path: pytest.TempPathFactory) -> None:
    """综合：任务外部成功但回报丢失，重连后查询真实状态不重试。

    1. ``TaskStore`` 中任务已 ``SUCCEEDED``（外部成功）；
    2. 重连后 ``RecoveryManager`` 查询到 succeeded → ``report_success``（非 resume）；
    3. ``TaskDeduplicator`` 拒绝重复执行；
    4. ``IdempotencyGuard`` 拒绝重复标记；
    5. 任务状态仍为 ``SUCCEEDED``，未回退。
    """
    store = TaskStore(str(tmp_path / "tasks.db"))
    _run(store.create(_make_task("t1", "k1")))
    _run(store.update_status("t1", TaskStatus.SUCCEEDED, b"ok"))

    # 重连恢复：远端真实状态为 succeeded
    server = AsyncMock()
    server.get_execution_state = AsyncMock(return_value="succeeded")
    mgr = RecoveryManager()
    actions = _run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="t1", execution_id="exec-t1")],
        )
    )
    assert actions[0].action == "report_success"

    # 去重器与幂等保护均拒绝重复执行
    dedup = TaskDeduplicator()
    assert dedup.should_execute("t1", "k1") is True  # 首次记录
    dedup.mark_complete("t1")
    assert dedup.is_completed("t1") is True
    assert dedup.should_execute("t1", "k1") is False  # 重复键拒绝

    guard = IdempotencyGuard()
    assert guard.check_and_mark("t1", "k1") is True
    assert guard.check_and_mark("t1", "k1") is False  # 重复拒绝

    # 任务状态仍为 SUCCEEDED，未回退
    fetched = _run(store.get("t1"))
    assert fetched.status is TaskStatus.SUCCEEDED
    _run(store.close())
