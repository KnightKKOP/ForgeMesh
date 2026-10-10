"""工作流执行引擎单元测试（对齐 development-plan 5.4）。

覆盖：
* 步骤正常执行；
* 幂等跳过（同一 key 第二次）；
* 重试后成功；
* 全部重试失败；
* 步骤级超时；
* 工作流级超时；
* 退避时间递增。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from forgemesh.workflow.runtime import RetryPolicy, Step, WorkflowDefinition
from forgemesh.workflowruntime.exec import (
    ExecutionError,
    StepExecutor,
    TimeoutLevel,
    WorkflowExecutor,
)


def _make_step(
    step_id: str = "s1",
    timeout: int | None = None,
) -> Step:
    """构造测试用步骤。"""
    return Step(id=step_id, uses="test.action", timeout=timeout)


def _make_definition(
    steps: list[Step] | None = None,
    timeout_seconds: int | None = None,
) -> WorkflowDefinition:
    """构造测试用工作流定义。"""
    return WorkflowDefinition(
        name="test-flow",
        timeout_seconds=timeout_seconds,
        steps=steps or [_make_step()],
    )


async def _ok_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
    """始终成功的 handler。"""
    return {"step_id": step.id, "status": "ok"}


# --------------------------------------------------------------------------- #
# execute_step
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_execute_step_success() -> None:
    """正常执行返回结果。"""
    executor = StepExecutor()
    step = _make_step()

    result = asyncio.run(
        executor.execute_step(step, {"x": 1}, "key-success", _ok_handler)
    )

    assert result == {"step_id": "s1", "status": "ok"}


@pytest.mark.unit
def test_execute_step_idempotent() -> None:
    """同一 key 第二次执行跳过。"""
    executor = StepExecutor()
    step = _make_step()

    result1 = asyncio.run(
        executor.execute_step(step, {}, "key-idem", _ok_handler)
    )
    result2 = asyncio.run(
        executor.execute_step(step, {}, "key-idem", _ok_handler)
    )

    assert result1 == {"step_id": "s1", "status": "ok"}
    assert result2 == {"status": "skipped", "reason": "idempotent"}


@pytest.mark.unit
def test_execute_step_retry_then_success() -> None:
    """第一次失败第二次成功（重试后成功）。"""
    call_count = 0

    async def flaky_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first attempt fails")
        return {"status": "ok", "attempt": call_count}

    # 退避设为 0 以加速测试。
    policy = RetryPolicy(
        max_attempts=3, initial_backoff_seconds=0.0, max_backoff_seconds=0.0
    )
    executor = StepExecutor(retry_policy=policy)
    step = _make_step()

    result = asyncio.run(
        executor.execute_step(step, {}, "key-retry", flaky_handler)
    )

    assert result == {"status": "ok", "attempt": 2}
    assert call_count == 2


@pytest.mark.unit
def test_execute_step_all_retries_fail() -> None:
    """全部重试失败抛异常。"""
    async def failing_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("always fails")

    policy = RetryPolicy(
        max_attempts=2, initial_backoff_seconds=0.0, max_backoff_seconds=0.0
    )
    executor = StepExecutor(retry_policy=policy)
    step = _make_step()

    with pytest.raises(RuntimeError, match="always fails"):
        asyncio.run(
            executor.execute_step(step, {}, "key-fail", failing_handler)
        )


@pytest.mark.unit
def test_execute_step_timeout() -> None:
    """步骤超时抛 ExecutionError(timeout_level=STEP)。"""
    async def slow_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"status": "ok"}

    policy = RetryPolicy(
        max_attempts=1, initial_backoff_seconds=0.0, max_backoff_seconds=0.0
    )
    executor = StepExecutor(retry_policy=policy)
    step = _make_step(timeout=1)

    with pytest.raises(ExecutionError) as exc_info:
        asyncio.run(
            executor.execute_step(step, {}, "key-timeout", slow_handler)
        )

    assert exc_info.value.timeout_level == TimeoutLevel.STEP
    assert "timed out" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# execute_workflow
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_execute_workflow_success() -> None:
    """工作流正常执行（多步骤顺序执行）。"""
    defn = _make_definition(
        steps=[_make_step("a"), _make_step("b")],
    )
    executor = WorkflowExecutor()

    results = asyncio.run(
        executor.execute_workflow(defn, {"input": 1}, "wf-key", _ok_handler)
    )

    assert set(results.keys()) == {"a", "b"}
    assert results["a"] == {"step_id": "a", "status": "ok"}
    assert results["b"] == {"step_id": "b", "status": "ok"}


@pytest.mark.unit
def test_execute_workflow_timeout() -> None:
    """工作流超时抛 ExecutionError(timeout_level=WORKFLOW)。"""
    async def slow_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"status": "ok"}

    defn = _make_definition(timeout_seconds=1)
    executor = WorkflowExecutor()

    with pytest.raises(ExecutionError) as exc_info:
        asyncio.run(
            executor.execute_workflow(defn, {}, "wf-timeout", slow_handler)
        )

    assert exc_info.value.timeout_level == TimeoutLevel.WORKFLOW
    assert "timed out" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 退避递增
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_retry_backoff() -> None:
    """退避时间递增（用 mock handler + time 记录）。"""
    call_times: list[float] = []

    async def failing_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        call_times.append(time.monotonic())
        raise RuntimeError("fail to trigger backoff")

    # 退避：initial=0.05, max=1.0, max_attempts=3
    # 退避序列：0.05, 0.1（第 0 次后等 0.05，第 1 次后等 0.1）
    policy = RetryPolicy(
        max_attempts=3, initial_backoff_seconds=0.05, max_backoff_seconds=1.0
    )
    executor = StepExecutor(retry_policy=policy)
    step = _make_step()

    with pytest.raises(RuntimeError):
        asyncio.run(
            executor.execute_step(step, {}, "key-backoff", failing_handler)
        )

    # 验证三次调用之间的间隔递增。
    assert len(call_times) == 3
    gap1 = call_times[1] - call_times[0]
    gap2 = call_times[2] - call_times[1]
    # gap1 ≈ 0.05, gap2 ≈ 0.1，gap2 应大于 gap1。
    assert gap2 > gap1
    # 间隔应接近预期退避值（允许一定误差）。
    assert gap1 >= 0.04
    assert gap2 >= 0.08
