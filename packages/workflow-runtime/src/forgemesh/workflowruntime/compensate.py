"""补偿步骤注册与反向执行。

在失败/取消时按反向拓扑序执行补偿，回滚已产生的副作用。
对齐 development-plan 5.4「补偿」语义。

设计要点
--------
* **反向拓扑序**：最后执行的步骤最先补偿（LIFO 语义），保证依赖关系正确回滚。
* **无补偿跳过**：未注册补偿 handler 的步骤标记为 ``SKIPPED``。
* **补偿失败不中断**：某步补偿失败标记为 ``COMPENSATION_FAILED``（异常记 WARNING 日志），
  仍继续补偿其余步骤，以最大化清理效果。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import StepStatus

logger = logging.getLogger(__name__)

#: 补偿 handler 类型：async (step, inputs) -> dict。
CompensationHandler = Callable[[Step, dict[str, Any]], Awaitable[dict[str, Any]]]


class CompensationRegistry:
    """补偿 handler 注册表：维护 step_id -> handler 映射。"""

    def __init__(self) -> None:
        self._handlers: dict[str, CompensationHandler] = {}

    def register(self, step_id: str, handler: CompensationHandler) -> None:
        """注册步骤的补偿 handler。

        Args:
            step_id: 步骤标识。
            handler: 异步补偿 handler ``async (step, inputs) -> dict``。
        """
        self._handlers[step_id] = handler

    def has_compensation(self, step_id: str) -> bool:
        """检查指定步骤是否注册了补偿 handler。"""
        return step_id in self._handlers

    async def compensate_step(
        self, step: Step, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        """执行指定步骤的补偿。

        Args:
            step: 步骤定义。
            inputs: 步骤执行结果（作为补偿输入）。

        Returns:
            补偿 handler 的返回值。

        Raises:
            KeyError: 未注册补偿 handler。
        """
        handler = self._handlers[step.id]
        return await handler(step, inputs)


class CompensationExecutor:
    """补偿执行器：按反向拓扑序执行已完成步骤的补偿。

    Args:
        registry: 补偿 handler 注册表。
    """

    def __init__(self, registry: CompensationRegistry | None = None) -> None:
        self._registry = registry or CompensationRegistry()

    @property
    def registry(self) -> CompensationRegistry:
        """补偿 handler 注册表。"""
        return self._registry

    async def compensate(
        self,
        defn: WorkflowDefinition,
        completed_steps: list[str],
        step_results: dict[str, Any],
    ) -> dict[str, StepStatus]:
        """按反向拓扑序执行已完成步骤的补偿。

        补偿按 ``completed_steps`` 的逆序执行（最后完成的先补偿），
        符合反向拓扑序语义。

        Args:
            defn: 工作流定义（用于查找 Step 对象）。
            completed_steps: 已完成步骤的 id 列表（按执行顺序）。
            step_results: 各步骤的执行结果 ``{step_id: result}``。

        Returns:
            各步骤补偿结果状态：
            * ``COMPENSATED`` —— 补偿成功；
            * ``COMPENSATION_FAILED`` —— 补偿 handler 抛异常；
            * ``SKIPPED`` —— 未注册补偿 handler 或步骤不在定义中。
        """
        results: dict[str, StepStatus] = {}
        step_map = {s.id: s for s in defn.steps}

        for step_id in reversed(completed_steps):
            step = step_map.get(step_id)
            if step is None:
                results[step_id] = StepStatus.SKIPPED
                continue
            if not self._registry.has_compensation(step_id):
                results[step_id] = StepStatus.SKIPPED
                continue
            try:
                await self._registry.compensate_step(
                    step, step_results.get(step_id, {})
                )
                results[step_id] = StepStatus.COMPENSATED
            except Exception:
                # 补偿失败不中断，继续补偿其余步骤以最大化清理。
                logger.warning("compensation of step %r failed", step_id, exc_info=True)
                results[step_id] = StepStatus.COMPENSATION_FAILED

        return results


__all__ = [
    "CompensationHandler",
    "CompensationRegistry",
    "CompensationExecutor",
]
