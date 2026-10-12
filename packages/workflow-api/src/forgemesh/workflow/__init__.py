"""ForgeMesh 工作流契约（命名空间包 ``forgemesh.workflow``）。

本包定义阶段 0 的 WorkEngine 公共契约：

* :mod:`forgemesh.workflow.states`  —— 工作流/步骤状态机与合法转移校验。
* :mod:`forgemesh.workflow.runtime` —— 可替换的 :class:`WorkflowRuntime` 运行契约
  （ADR-003：引擎可替换，语义不随引擎改变）。

对齐 ``docs/architecture/development-plan.md`` 5.4 / 6.3。
"""

from __future__ import annotations

from .runtime import (
    RetryPolicy,
    RunInfo,
    RunRef,
    Step,
    WorkflowDefinition,
    WorkflowRuntime,
)
from .states import (
    STEP_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    StepStatus,
    WorkflowStatus,
    validate_step_transition,
    validate_workflow_transition,
)

__all__ = [
    # states
    "WorkflowStatus",
    "StepStatus",
    "WORKFLOW_TRANSITIONS",
    "STEP_TRANSITIONS",
    "validate_workflow_transition",
    "validate_step_transition",
    # runtime
    "Step",
    "RetryPolicy",
    "WorkflowDefinition",
    "RunRef",
    "RunInfo",
    "WorkflowRuntime",
]
