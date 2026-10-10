"""ForgeMesh 工作流状态机（对齐 development-plan 5.4 / 6.3）。

本模块是**运行时权威**：合法状态转移由运行时校验，插件不得自定义。

两个状态机：

* :class:`WorkflowStatus` —— 工作流级 8 态。
* :class:`StepStatus`     —— 步骤级 10 态（含补偿语义）。

非法转移统一抛出 :class:`forgemesh.core.errors.InvalidStateTransition`。
若核心包尚未安装，则回退到本模块内定义的同名兜底异常类，保证契约可独立加载。

兼容 Python 3.9+。
"""

from __future__ import annotations

from enum import StrEnum

try:  # 优先复用核心包的统一异常体系（阶段 0 契约）
    from forgemesh.core.errors import InvalidStateTransition
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class InvalidStateTransition(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的非法状态转移异常（接口与核心包保持一致）。"""

        def __init__(self, message: str = "", details: dict = None) -> None:
            super().__init__(message)
            self.message = message
            self.details = details


class WorkflowStatus(StrEnum):
    """工作流运行状态（8 态）。

    终态（无出边）：``SUCCEEDED`` / ``FAILED`` / ``CANCELLED`` / ``TIMEDOUT``。
    """

    PENDING = "Pending"
    RUNNING = "Running"
    WAITING = "Waiting"
    COMPENSATING = "Compensating"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    TIMEDOUT = "TimedOut"


class StepStatus(StrEnum):
    """步骤运行状态（10 态，含补偿语义）。

    终态（无出边）：``SKIPPED`` / ``COMPENSATED`` / ``COMPENSATION_FAILED`` / ``CANCELLED``。
    ``SUCCEEDED`` 仍可转入 ``COMPENSATING``（取消/失败时对已成功步骤反向补偿）。
    """

    PENDING = "Pending"
    RUNNING = "Running"
    RETRYING = "Retrying"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    SKIPPED = "Skipped"
    COMPENSATING = "Compensating"
    COMPENSATED = "Compensated"
    COMPENSATION_FAILED = "CompensationFailed"
    CANCELLED = "Cancelled"


# 合法状态转移（由运行时校验，插件不可自定义，对齐 development-plan 5.4 / 6.3）。
# 未列出的状态视为终态（无出边）。
WORKFLOW_TRANSITIONS: dict[WorkflowStatus, set[WorkflowStatus]] = {
    WorkflowStatus.PENDING: {
        WorkflowStatus.RUNNING,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.RUNNING: {
        WorkflowStatus.WAITING,
        WorkflowStatus.COMPENSATING,
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMEDOUT,
    },
    WorkflowStatus.WAITING: {
        WorkflowStatus.RUNNING,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMEDOUT,
    },
    WorkflowStatus.COMPENSATING: {
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    # 终态：无出边
    WorkflowStatus.SUCCEEDED: set(),
    WorkflowStatus.FAILED: set(),
    WorkflowStatus.CANCELLED: set(),
    WorkflowStatus.TIMEDOUT: set(),
}

# 步骤级合法转移（对齐 development-plan 5.4 / 6.3 的状态枚举）。
STEP_TRANSITIONS: dict[StepStatus, set[StepStatus]] = {
    StepStatus.PENDING: {
        StepStatus.RUNNING,
        StepStatus.SKIPPED,
        StepStatus.CANCELLED,
    },
    StepStatus.RUNNING: {
        StepStatus.RETRYING,
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.COMPENSATING,
        StepStatus.CANCELLED,
    },
    StepStatus.RETRYING: {
        StepStatus.RUNNING,
        StepStatus.FAILED,
        StepStatus.CANCELLED,
    },
    # 已成功的步骤在取消/失败场景下可被反向补偿
    StepStatus.SUCCEEDED: {
        StepStatus.COMPENSATING,
    },
    StepStatus.FAILED: {
        StepStatus.COMPENSATING,
        StepStatus.CANCELLED,
    },
    # 终态：无出边
    StepStatus.SKIPPED: set(),
    StepStatus.COMPENSATING: {
        StepStatus.COMPENSATED,
        StepStatus.COMPENSATION_FAILED,
    },
    StepStatus.COMPENSATED: set(),
    StepStatus.COMPENSATION_FAILED: set(),
    StepStatus.CANCELLED: set(),
}


def _coerce_workflow(status: WorkflowStatus | str) -> WorkflowStatus:
    """将输入规范化为 :class:`WorkflowStatus`；未知取值抛 InvalidStateTransition。"""
    if isinstance(status, WorkflowStatus):
        return status
    try:
        return WorkflowStatus(status)
    except ValueError as exc:  # 未知状态名
        raise InvalidStateTransition(f"unknown workflow status: {status!r}") from exc


def _coerce_step(status: StepStatus | str) -> StepStatus:
    """将输入规范化为 :class:`StepStatus`；未知取值抛 InvalidStateTransition。"""
    if isinstance(status, StepStatus):
        return status
    try:
        return StepStatus(status)
    except ValueError as exc:
        raise InvalidStateTransition(f"unknown step status: {status!r}") from exc


def validate_workflow_transition(
    cur: WorkflowStatus | str, to: WorkflowStatus | str
) -> None:
    """校验工作流状态转移是否合法。

    Args:
        cur: 当前状态。
        to: 目标状态。

    Raises:
        InvalidStateTransition: 当 ``cur -> to`` 不在 :data:`WORKFLOW_TRANSITIONS` 中，
            或状态取值未知时抛出。
    """
    cur_s = _coerce_workflow(cur)
    to_s = _coerce_workflow(to)
    allowed = WORKFLOW_TRANSITIONS.get(cur_s, set())
    if to_s not in allowed:
        raise InvalidStateTransition(
            f"illegal workflow transition: {cur_s.value} -> {to_s.value}"
        )


def validate_step_transition(
    cur: StepStatus | str, to: StepStatus | str
) -> None:
    """校验步骤状态转移是否合法。

    Args:
        cur: 当前步骤状态。
        to: 目标步骤状态。

    Raises:
        InvalidStateTransition: 当 ``cur -> to`` 不在 :data:`STEP_TRANSITIONS` 中，
            或状态取值未知时抛出。
    """
    cur_s = _coerce_step(cur)
    to_s = _coerce_step(to)
    allowed = STEP_TRANSITIONS.get(cur_s, set())
    if to_s not in allowed:
        raise InvalidStateTransition(
            f"illegal step transition: {cur_s.value} -> {to_s.value}"
        )


__all__ = [
    "WorkflowStatus",
    "StepStatus",
    "WORKFLOW_TRANSITIONS",
    "STEP_TRANSITIONS",
    "validate_workflow_transition",
    "validate_step_transition",
    "InvalidStateTransition",
]
