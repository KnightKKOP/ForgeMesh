"""参考工作流运行时插件（实现 WorkflowRuntime 契约）。

执行引擎是 :class:`forgemesh.workflowruntime.runtime.LocalWorkflowRuntime`：组合
WorkflowStore + WorkflowExecutor + CompensationExecutor + ApprovalGate，在后台执行运行
实例，失败时补偿，重启后恢复（语义见该模块说明）。本插件以子类提供同一引擎，供插件
管理器按清单加载；控制平面不导入插件模块，直接使用 ``forgemesh.workflowruntime`` 中的
实现。

对齐 development-plan 5.4 / ADR-003。本插件不产出资源终态（INV-1/INV-3），
任何状态写入必须经 Registry 由 Controller 完成。
"""

from __future__ import annotations

from forgemesh.workflowruntime.runtime import LocalWorkflowRuntime, StepHandler


class ReferenceWorkflowRuntime(LocalWorkflowRuntime):
    """参考工作流运行时：行为与 :class:`LocalWorkflowRuntime` 相同。

    Args:
        store: 工作流持久化存储。
        executor: 工作流执行器。
        compensation: 补偿执行器。
        approval: 审批门控。
        handler: 步骤执行 handler ``async (step, inputs) -> dict``。
    """


__all__ = [
    "ReferenceWorkflowRuntime",
    "StepHandler",
]
