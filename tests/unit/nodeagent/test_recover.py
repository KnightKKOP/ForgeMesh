"""``forgemesh.nodeagent.recover`` 单元测试。

覆盖断线重连与任务恢复：

* recover 根据远端真实状态决策 resume/cancel/report_success/report_failure；
* 远端未知状态乐观 resume；
* IdempotencyGuard 首次返回 True、重复返回 False。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.nodeagent.recover import (
    IdempotencyGuard,
    RecoveryAction,
    RecoveryManager,
    RunningExecution,
)
from forgemesh.nodeagent.server import AgentServer

pytestmark = pytest.mark.unit


def _make_server_with_states(states: dict[str, str]) -> AgentServer:
    """构造已注册 agent 并注入执行状态的 server。"""
    server = AgentServer()
    asyncio.run(
        server.register(
            agent_id="agent-1",
            agent_version="0.1.0",
            capabilities=["nvidia-gpu"],
            supported_task_types=["PROBE_GPU"],
            core_api_version="v1",
        )
    )
    for eid, state in states.items():
        server.set_execution_state(eid, state)
    return server


def test_recover_resume_when_running() -> None:
    """远端仍 running → resume。"""
    server = _make_server_with_states({"exec-1": "running"})
    mgr = RecoveryManager()

    actions = asyncio.run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="task-1", execution_id="exec-1")],
        )
    )

    assert len(actions) == 1
    assert actions[0].action == "resume"
    assert actions[0].task_id == "task-1"


def test_recover_report_success_when_succeeded() -> None:
    """远端已 succeeded → report_success。"""
    server = _make_server_with_states({"exec-1": "succeeded"})
    mgr = RecoveryManager()

    actions = asyncio.run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="task-1", execution_id="exec-1")],
        )
    )

    assert actions[0].action == "report_success"


def test_recover_report_failure_when_failed() -> None:
    """远端已 failed → report_failure。"""
    server = _make_server_with_states({"exec-1": "failed"})
    mgr = RecoveryManager()

    actions = asyncio.run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="task-1", execution_id="exec-1")],
        )
    )

    assert actions[0].action == "report_failure"


def test_recover_cancel_when_cancelled() -> None:
    """远端已 cancelled → cancel。"""
    server = _make_server_with_states({"exec-1": "cancelled"})
    mgr = RecoveryManager()

    actions = asyncio.run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="task-1", execution_id="exec-1")],
        )
    )

    assert actions[0].action == "cancel"


def test_recover_resume_when_unknown() -> None:
    """远端状态未知 → 乐观 resume。"""
    server = _make_server_with_states({})
    mgr = RecoveryManager()

    actions = asyncio.run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[RunningExecution(task_id="task-1", execution_id="exec-ghost")],
        )
    )

    assert actions[0].action == "resume"
    assert "unknown" in actions[0].reason


def test_recover_multiple_executions() -> None:
    """多个在途执行应逐个决策，顺序保留。"""
    server = _make_server_with_states(
        {"exec-1": "succeeded", "exec-2": "running", "exec-3": "cancelled"}
    )
    mgr = RecoveryManager()

    actions = asyncio.run(
        mgr.recover(
            server,
            agent_id="agent-1",
            running_executions=[
                RunningExecution(task_id="t1", execution_id="exec-1"),
                RunningExecution(task_id="t2", execution_id="exec-2"),
                RunningExecution(task_id="t3", execution_id="exec-3"),
            ],
        )
    )

    assert [a.action for a in actions] == ["report_success", "resume", "cancel"]
    assert [a.task_id for a in actions] == ["t1", "t2", "t3"]


def test_recovery_action_is_frozen() -> None:
    """RecoveryAction 为 frozen dataclass。"""
    a = RecoveryAction(task_id="t", action="resume", reason="r")
    with pytest.raises(AttributeError):
        a.action = "cancel"  # type: ignore[misc]


def test_idempotency_first_then_duplicate() -> None:
    """首次 check_and_mark 返回 True，重复返回 False。"""
    guard = IdempotencyGuard()

    assert guard.check_and_mark("task-1", "key-1") is True
    assert guard.check_and_mark("task-1", "key-1") is False


def test_idempotency_different_keys() -> None:
    """不同 task_id / idempotency_key 组合互不影响。"""
    guard = IdempotencyGuard()

    assert guard.check_and_mark("task-1", "key-a") is True
    assert guard.check_and_mark("task-1", "key-b") is True
    assert guard.check_and_mark("task-2", "key-a") is True
    assert guard.check_and_mark("task-1", "key-a") is False
