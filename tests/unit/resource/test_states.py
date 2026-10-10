"""状态机与转移校验单元测试。

覆盖（对齐 development-plan 5.1 / 7.2）：

* ``InferenceService`` 合法转移链（Pending→...→Ready）通过；
* **非法转移被拒**（如 Ready→Scheduling 抛 ``InvalidStateTransition``）；
* ``Ready`` 到 ``Degraded`` / ``Updating`` / ``Terminating`` 的双向/单向语义；
* ``Terminating`` / ``Failed`` / ``Unknown`` 终态无出边；
* ``Allocation`` 的 ``Active → {Released, Expired}`` 单向语义；
* 自定义 ``StateMachine`` 的 ``can`` / ``validate`` 行为。
"""

from __future__ import annotations

import pytest
from forgemesh.core.errors import InvalidStateTransition
from forgemesh.resource.allocation import AllocationStatus
from forgemesh.resource.inference import InferencePhase
from forgemesh.resource.states import (
    ALLOCATION_STATE_MACHINE,
    ALLOCATION_TRANSITIONS,
    INFERENCE_STATE_MACHINE,
    INFERENCE_TRANSITIONS,
    StateMachine,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 推理服务状态机
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (InferencePhase.PENDING, InferencePhase.SCHEDULING),
        (InferencePhase.SCHEDULING, InferencePhase.PREPARING),
        (InferencePhase.PREPARING, InferencePhase.DEPLOYING),
        (InferencePhase.DEPLOYING, InferencePhase.VERIFYING),
        (InferencePhase.VERIFYING, InferencePhase.READY),
        (InferencePhase.READY, InferencePhase.DEGRADED),
        (InferencePhase.DEGRADED, InferencePhase.READY),
        (InferencePhase.READY, InferencePhase.UPDATING),
        (InferencePhase.UPDATING, InferencePhase.READY),
        (InferencePhase.READY, InferencePhase.TERMINATING),
    ],
)
def test_inference_legal_transitions_pass(
    current: InferencePhase, target: InferencePhase
) -> None:
    """合法转移：``can`` 为 True 且 ``validate`` 不抛异常。"""
    assert INFERENCE_STATE_MACHINE.can(current.value, target.value) is True
    INFERENCE_STATE_MACHINE.validate(current.value, target.value)  # 不抛异常


def test_ready_to_scheduling_is_rejected() -> None:
    """非法转移 Ready→Scheduling 必须抛 InvalidStateTransition。"""
    with pytest.raises(InvalidStateTransition):
        INFERENCE_STATE_MACHINE.validate(
            InferencePhase.READY.value, InferencePhase.SCHEDULING.value
        )
    assert (
        INFERENCE_STATE_MACHINE.can(
            InferencePhase.READY.value, InferencePhase.SCHEDULING.value
        )
        is False
    )


def test_backward_transition_is_rejected() -> None:
    """不允许回退：Scheduling→Pending 被拒。"""
    with pytest.raises(InvalidStateTransition):
        INFERENCE_STATE_MACHINE.validate(
            InferencePhase.SCHEDULING.value, InferencePhase.PENDING.value
        )


@pytest.mark.parametrize(
    "source",
    [
        InferencePhase.PENDING,
        InferencePhase.SCHEDULING,
        InferencePhase.PREPARING,
        InferencePhase.DEPLOYING,
        InferencePhase.VERIFYING,
        InferencePhase.READY,
        InferencePhase.DEGRADED,
        InferencePhase.UPDATING,
    ],
)
@pytest.mark.parametrize("sink", [InferencePhase.FAILED, InferencePhase.UNKNOWN])
def test_any_non_terminal_state_can_fail(
    source: InferencePhase, sink: InferencePhase
) -> None:
    """除终止/吸收态外，任意状态均可转移到 Failed / Unknown。"""
    INFERENCE_STATE_MACHINE.validate(source.value, sink.value)


@pytest.mark.parametrize(
    "terminal",
    [InferencePhase.TERMINATING, InferencePhase.FAILED, InferencePhase.UNKNOWN],
)
def test_terminal_states_have_no_outgoing(terminal: InferencePhase) -> None:
    """终态 / 吸收态无出边，任何转移都被拒。"""
    assert INFERENCE_TRANSITIONS[terminal.value] == set()
    for target in InferencePhase:
        with pytest.raises(InvalidStateTransition):
            INFERENCE_STATE_MACHINE.validate(terminal.value, target.value)


def test_unknown_source_state_is_rejected() -> None:
    """未知源状态一律视为非法。"""
    assert INFERENCE_STATE_MACHINE.can("NotAState", InferencePhase.READY.value) is False
    with pytest.raises(InvalidStateTransition):
        INFERENCE_STATE_MACHINE.validate("NotAState", InferencePhase.READY.value)


# ---------------------------------------------------------------------------
# 分配状态机
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target", [AllocationStatus.RELEASED, AllocationStatus.EXPIRED]
)
def test_allocation_active_transitions_allowed(target: AllocationStatus) -> None:
    """Active → {Released, Expired} 为合法转移。"""
    ALLOCATION_STATE_MACHINE.validate(AllocationStatus.ACTIVE.value, target.value)


def test_allocation_released_to_active_is_rejected() -> None:
    """Released 为终态，不能回到 Active。"""
    with pytest.raises(InvalidStateTransition):
        ALLOCATION_STATE_MACHINE.validate(
            AllocationStatus.RELEASED.value, AllocationStatus.ACTIVE.value
        )


def test_allocation_expired_has_no_outgoing() -> None:
    """Expired 无出边。"""
    assert ALLOCATION_TRANSITIONS[AllocationStatus.EXPIRED.value] == set()
    with pytest.raises(InvalidStateTransition):
        ALLOCATION_STATE_MACHINE.validate(
            AllocationStatus.EXPIRED.value, AllocationStatus.RELEASED.value
        )


# ---------------------------------------------------------------------------
# 通用 StateMachine 行为
# ---------------------------------------------------------------------------


def test_state_machine_copies_transition_sets() -> None:
    """构造后外部修改原字典不应影响实例（防御性复制）。"""
    transitions = {"A": {"B"}}
    machine = StateMachine(states={"A", "B"}, transitions=transitions, name="demo")

    transitions["A"].add("C")

    assert machine.can("A", "B") is True
    assert machine.can("A", "C") is False


def test_state_machine_validate_raises_with_details() -> None:
    """非法转移异常应携带机器名与允许集合，便于诊断。"""
    machine = StateMachine(
        states={"A", "B", "C"}, transitions={"A": {"B"}}, name="demo"
    )
    with pytest.raises(InvalidStateTransition) as excinfo:
        machine.validate("A", "C")

    assert "demo" in str(excinfo.value)
    assert excinfo.value.details is not None
    assert excinfo.value.details["allowed"] == ["B"]
