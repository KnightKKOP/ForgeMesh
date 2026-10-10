"""工作流/步骤状态机单元测试（对齐 development-plan 5.4 / 第 8 章 8.1）。

覆盖：
* 合法转移通过；
* 非法转移（如 ``SUCCEEDED -> RUNNING``）抛 ``InvalidStateTransition``；
* :class:`StepStatus` 恰好 10 态、:class:`WorkflowStatus` 恰好 8 态。

兼容 Python 3.9+。
"""

from __future__ import annotations

import pytest
from forgemesh.workflow.states import (
    STEP_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    InvalidStateTransition,
    StepStatus,
    WorkflowStatus,
    validate_step_transition,
    validate_workflow_transition,
)


# --------------------------------------------------------------------------- #
# 枚举规模
# --------------------------------------------------------------------------- #
def test_step_status_has_ten_states() -> None:
    """StepStatus 必须恰好为 10 态（对齐 5.4）。"""
    assert len(list(StepStatus)) == 10


def test_workflow_status_has_eight_states() -> None:
    """WorkflowStatus 必须恰好为 8 态（对齐 5.4）。"""
    assert len(list(WorkflowStatus)) == 8


# --------------------------------------------------------------------------- #
# 工作流状态转移
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("cur", "to"),
    [
        (WorkflowStatus.PENDING, WorkflowStatus.RUNNING),
        (WorkflowStatus.PENDING, WorkflowStatus.CANCELLED),
        (WorkflowStatus.RUNNING, WorkflowStatus.WAITING),
        (WorkflowStatus.RUNNING, WorkflowStatus.COMPENSATING),
        (WorkflowStatus.RUNNING, WorkflowStatus.SUCCEEDED),
        (WorkflowStatus.RUNNING, WorkflowStatus.FAILED),
        (WorkflowStatus.RUNNING, WorkflowStatus.TIMEDOUT),
        (WorkflowStatus.WAITING, WorkflowStatus.RUNNING),
    ],
)
def test_valid_workflow_transitions(cur: WorkflowStatus, to: WorkflowStatus) -> None:
    """合法工作流转移不应抛出异常。"""
    assert validate_workflow_transition(cur, to) is None


@pytest.mark.parametrize(
    ("cur", "to"),
    [
        (WorkflowStatus.SUCCEEDED, WorkflowStatus.RUNNING),  # 终态无出边
        (WorkflowStatus.FAILED, WorkflowStatus.RUNNING),
        (WorkflowStatus.CANCELLED, WorkflowStatus.RUNNING),
        (WorkflowStatus.TIMEDOUT, WorkflowStatus.RUNNING),
        (WorkflowStatus.PENDING, WorkflowStatus.SUCCEEDED),  # 未运行不可直达成功
    ],
)
def test_invalid_workflow_transitions(cur: WorkflowStatus, to: WorkflowStatus) -> None:
    """非法工作流转移必须抛 InvalidStateTransition。"""
    with pytest.raises(InvalidStateTransition):
        validate_workflow_transition(cur, to)


def test_workflow_transition_accepts_str() -> None:
    """接受字符串取值（内部规范化）。"""
    assert validate_workflow_transition("Pending", "Running") is None


def test_workflow_transition_unknown_status_raises() -> None:
    """未知状态取值同样抛 InvalidStateTransition。"""
    with pytest.raises(InvalidStateTransition):
        validate_workflow_transition("NotAStatus", WorkflowStatus.RUNNING)


def test_workflow_terminal_states_have_no_outgoing_edges() -> None:
    """终态在转移表中无出边。"""
    for terminal in (
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMEDOUT,
    ):
        assert WORKFLOW_TRANSITIONS[terminal] == set()


# --------------------------------------------------------------------------- #
# 步骤状态转移
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("cur", "to"),
    [
        (StepStatus.PENDING, StepStatus.RUNNING),
        (StepStatus.PENDING, StepStatus.SKIPPED),
        (StepStatus.RUNNING, StepStatus.RETRYING),
        (StepStatus.RUNNING, StepStatus.SUCCEEDED),
        (StepStatus.RETRYING, StepStatus.RUNNING),
        (StepStatus.SUCCEEDED, StepStatus.COMPENSATING),
        (StepStatus.FAILED, StepStatus.COMPENSATING),
        (StepStatus.COMPENSATING, StepStatus.COMPENSATED),
        (StepStatus.COMPENSATING, StepStatus.COMPENSATION_FAILED),
    ],
)
def test_valid_step_transitions(cur: StepStatus, to: StepStatus) -> None:
    """合法步骤转移不应抛出异常。"""
    assert validate_step_transition(cur, to) is None


@pytest.mark.parametrize(
    ("cur", "to"),
    [
        (StepStatus.SUCCEEDED, StepStatus.RUNNING),  # 终态语义：不可重跑
        (StepStatus.SKIPPED, StepStatus.RUNNING),
        (StepStatus.COMPENSATED, StepStatus.RUNNING),
        (StepStatus.COMPENSATION_FAILED, StepStatus.COMPENSATING),
        (StepStatus.CANCELLED, StepStatus.RUNNING),
    ],
)
def test_invalid_step_transitions(cur: StepStatus, to: StepStatus) -> None:
    """非法步骤转移必须抛 InvalidStateTransition。"""
    with pytest.raises(InvalidStateTransition):
        validate_step_transition(cur, to)


def test_step_terminal_states_have_no_outgoing_edges() -> None:
    """步骤终态在转移表中无出边。"""
    for terminal in (
        StepStatus.SKIPPED,
        StepStatus.COMPENSATED,
        StepStatus.COMPENSATION_FAILED,
        StepStatus.CANCELLED,
    ):
        assert STEP_TRANSITIONS[terminal] == set()


def test_all_statuses_declared_in_transition_tables() -> None:
    """转移表必须覆盖所有状态（防止遗漏）。"""
    assert set(WORKFLOW_TRANSITIONS) == set(WorkflowStatus)
    assert set(STEP_TRANSITIONS) == set(StepStatus)
