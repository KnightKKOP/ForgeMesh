"""工作流执行引擎：幂等/重试/三级超时。

对齐 development-plan 5.4。三级超时区分：

* **步骤超时**（``Step.timeout``）：单个步骤执行的时间上限。
* **工作流超时**（``WorkflowDefinition.timeout_seconds``）：整个工作流执行的时间上限。
* **外部任务超时**（由 Provider 声明）：不在本模块范围内，由具体 Provider 实现。

执行语义
--------
* **幂等**：同一 ``idempotency_key`` 的步骤只执行一次，重复调用返回跳过结果。
* **重试**：依据 :class:`RetryPolicy` 按指数退避重试，达到 ``max_attempts`` 后抛出
  最后一次错误。
* **超时**：步骤超时抛 :class:`ExecutionError`（``timeout_level=STEP``）；
  工作流超时抛 :class:`ExecutionError`（``timeout_level=WORKFLOW``）。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
from typing import Any

from forgemesh.workflow.runtime import RetryPolicy, Step, WorkflowDefinition


class TimeoutLevel:
    """三级超时标识。"""

    STEP = "step"
    WORKFLOW = "workflow"
    EXTERNAL = "external"


class ExecutionError(Exception):
    """执行错误（含超时级别）。

    Attributes:
        timeout_level: 超时级别（``"step"`` / ``"workflow"`` / ``"external"``），
            非超时错误为 ``None``。
    """

    def __init__(self, message: str, timeout_level: str | None = None) -> None:
        super().__init__(message)
        self.timeout_level = timeout_level


class StepExecutor:
    """步骤执行器：幂等执行 + 重试 + 步骤级超时。

    Args:
        retry_policy: 重试策略；``None`` 时使用默认 :class:`RetryPolicy`。
    """

    def __init__(self, retry_policy: RetryPolicy | None = None) -> None:
        self._retry = retry_policy or RetryPolicy()
        #: 已执行的幂等键缓存（同一 key 不重复执行）。
        self._executed_keys: set[str] = set()

    async def execute_step(
        self,
        step: Step,
        inputs: dict[str, Any],
        idempotency_key: str,
        handler: Any,
    ) -> dict[str, Any]:
        """执行单个步骤（幂等 + 重试 + 步骤级超时）。

        Args:
            step: 步骤定义。
            inputs: 步骤输入（含上游步骤结果）。
            idempotency_key: 幂等键；同一 key 重复调用返回跳过结果。
            handler: 异步可调用 ``async (step, inputs) -> dict``。

        Returns:
            步骤执行结果字典。幂等跳过时返回 ``{"status": "skipped", "reason": "idempotent"}``。

        Raises:
            ExecutionError: 步骤超时（``timeout_level=STEP``）或全部重试失败。
        """
        # 幂等检查：同一 idempotency_key 已执行则跳过。
        if idempotency_key in self._executed_keys:
            return {"status": "skipped", "reason": "idempotent"}

        last_error: Exception | None = None
        for attempt in range(self._retry.max_attempts):
            try:
                # 步骤级超时。
                if step.timeout:
                    result = await asyncio.wait_for(
                        handler(step, inputs),
                        timeout=step.timeout,
                    )
                else:
                    result = await handler(step, inputs)
                self._executed_keys.add(idempotency_key)
                return result
            except TimeoutError:
                last_error = ExecutionError(
                    f"step '{step.id}' timed out after {step.timeout}s",
                    timeout_level=TimeoutLevel.STEP,
                )
            except Exception as exc:
                last_error = exc

            # 指数退避（非最后一次尝试时）。
            if attempt < self._retry.max_attempts - 1:
                backoff = min(
                    self._retry.initial_backoff_seconds * (2**attempt),
                    self._retry.max_backoff_seconds,
                )
                if backoff > 0:
                    await asyncio.sleep(backoff)

        raise last_error or ExecutionError(f"step '{step.id}' failed")

    def reset(self) -> None:
        """重置幂等键缓存（用于测试或重新初始化）。"""
        self._executed_keys.clear()


class WorkflowExecutor:
    """工作流执行器：管理步骤执行 + 工作流级超时。

    Args:
        step_executor: 步骤执行器；``None`` 时使用默认 :class:`StepExecutor`。
    """

    def __init__(self, step_executor: StepExecutor | None = None) -> None:
        self._step_executor = step_executor or StepExecutor()

    async def execute_workflow(
        self,
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
        idempotency_key: str,
        handler: Any,
    ) -> dict[str, Any]:
        """执行工作流（含工作流级超时）。

        按定义中步骤顺序依次执行，每一步的输入包含原始输入与已执行步骤的结果。
        若 ``defn.timeout_seconds`` 不为 ``None``，则整个工作流受超时约束。

        Args:
            defn: 工作流定义。
            inputs: 工作流输入。
            idempotency_key: 幂等键。
            handler: 异步可调用 ``async (step, inputs) -> dict``。

        Returns:
            各步骤结果字典 ``{step_id: result}``。

        Raises:
            ExecutionError: 工作流超时（``timeout_level=WORKFLOW``）或步骤执行失败。
        """

        async def _run() -> dict[str, Any]:
            results: dict[str, Any] = {}
            for step in defn.steps:
                step_key = f"{idempotency_key}:{step.id}"
                result = await self._step_executor.execute_step(
                    step, {**inputs, **results}, step_key, handler
                )
                results[step.id] = result
            return results

        if defn.timeout_seconds:
            try:
                return await asyncio.wait_for(_run(), timeout=defn.timeout_seconds)
            except TimeoutError as exc:
                raise ExecutionError(
                    f"workflow '{defn.name}' timed out after {defn.timeout_seconds}s",
                    timeout_level=TimeoutLevel.WORKFLOW,
                ) from exc
        return await _run()


__all__ = [
    "TimeoutLevel",
    "ExecutionError",
    "StepExecutor",
    "WorkflowExecutor",
]
