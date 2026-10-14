"""补偿步骤注册与反向执行单元测试（对齐 development-plan 5.4）。

覆盖：
* 注册/查找/执行补偿 handler；
* 反向拓扑序执行；
* 补偿成功/失败；
* 无补偿 handler 的步骤跳过。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import StepStatus
from forgemesh.workflowruntime.compensate import (
    CompensationExecutor,
    CompensationRegistry,
)


def _make_definition() -> WorkflowDefinition:
    """构造三步骤工作流定义（a → b → c）。"""
    return WorkflowDefinition(
        name="test-flow",
        steps=[
            Step(id="a", uses="action.a"),
            Step(id="b", uses="action.b", needs=["a"]),
            Step(id="c", uses="action.c", needs=["b"]),
        ],
    )


# --------------------------------------------------------------------------- #
# CompensationRegistry
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_register_and_has_compensation() -> None:
    """注册后 has_compensation 返回 True。"""

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"compensated": step.id}

    registry = CompensationRegistry()
    assert not registry.has_compensation("a")

    registry.register("a", handler)
    assert registry.has_compensation("a")
    assert not registry.has_compensation("b")


@pytest.mark.unit
def test_compensate_step_executes() -> None:
    """compensate_step 执行注册的 handler。"""
    registry = CompensationRegistry()
    call_log: list[str] = []

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        call_log.append(step.id)
        return {"undone": step.id}

    registry.register("a", handler)
    step = Step(id="a", uses="action.a")
    result = asyncio.run(registry.compensate_step(step, {"output": "ok"}))

    assert result == {"undone": "a"}
    assert call_log == ["a"]


@pytest.mark.unit
def test_compensate_step_no_handler_raises() -> None:
    """未注册 handler 时 compensate_step 抛 KeyError。"""
    registry = CompensationRegistry()
    step = Step(id="unknown", uses="action.x")
    with pytest.raises(KeyError):
        asyncio.run(registry.compensate_step(step, {}))


# --------------------------------------------------------------------------- #
# CompensationExecutor — 反向拓扑序
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_compensate_reverse_order() -> None:
    """补偿按反向拓扑序执行（c → b → a）。"""
    executor = CompensationExecutor()
    call_order: list[str] = []

    async def make_handler(sid: str):
        async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
            call_order.append(sid)
            return {"undone": sid}

        return handler

    # 注册所有步骤的补偿 handler。
    executor.registry.register("a", asyncio.run(make_handler("a")))
    executor.registry.register("b", asyncio.run(make_handler("b")))
    executor.registry.register("c", asyncio.run(make_handler("c")))

    defn = _make_definition()
    completed = ["a", "b", "c"]
    step_results = {"a": {"r": 1}, "b": {"r": 2}, "c": {"r": 3}}

    results = asyncio.run(executor.compensate(defn, completed, step_results))

    # 反向序：c → b → a。
    assert call_order == ["c", "b", "a"]
    assert results["a"] == StepStatus.COMPENSATED
    assert results["b"] == StepStatus.COMPENSATED
    assert results["c"] == StepStatus.COMPENSATED


# --------------------------------------------------------------------------- #
# 补偿成功
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_compensate_success() -> None:
    """补偿成功返回 COMPENSATED。"""
    executor = CompensationExecutor()

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"undone": step.id}

    executor.registry.register("a", handler)

    defn = WorkflowDefinition(name="flow", steps=[Step(id="a", uses="x")])
    results = asyncio.run(executor.compensate(defn, ["a"], {"a": {}}))

    assert results["a"] == StepStatus.COMPENSATED


# --------------------------------------------------------------------------- #
# 补偿失败
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_compensate_failure() -> None:
    """补偿 handler 抛异常时返回 COMPENSATION_FAILED。"""
    executor = CompensationExecutor()

    async def failing_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("compensation failed")

    executor.registry.register("a", failing_handler)

    defn = WorkflowDefinition(name="flow", steps=[Step(id="a", uses="x")])
    results = asyncio.run(executor.compensate(defn, ["a"], {"a": {}}))

    assert results["a"] == StepStatus.COMPENSATION_FAILED


@pytest.mark.unit
def test_compensate_failure_does_not_stop_others() -> None:
    """某步补偿失败不中断其余步骤的补偿。"""
    executor = CompensationExecutor()
    call_log: list[str] = []

    async def failing_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        call_log.append(step.id)
        raise RuntimeError("fail")

    async def ok_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        call_log.append(step.id)
        return {"undone": step.id}

    executor.registry.register("a", ok_handler)
    executor.registry.register("b", failing_handler)

    defn = WorkflowDefinition(
        name="flow",
        steps=[Step(id="a", uses="x"), Step(id="b", uses="x", needs=["a"])],
    )
    # 执行顺序 a → b，补偿反向 b → a。
    results = asyncio.run(executor.compensate(defn, ["a", "b"], {}))

    assert call_log == ["b", "a"]
    assert results["b"] == StepStatus.COMPENSATION_FAILED
    assert results["a"] == StepStatus.COMPENSATED


# --------------------------------------------------------------------------- #
# 无补偿跳过
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_compensate_no_handler_skipped() -> None:
    """未注册补偿 handler 的步骤标记为 SKIPPED。"""
    executor = CompensationExecutor()
    defn = _make_definition()

    results = asyncio.run(executor.compensate(defn, ["a", "b", "c"], {}))

    assert results["a"] == StepStatus.SKIPPED
    assert results["b"] == StepStatus.SKIPPED
    assert results["c"] == StepStatus.SKIPPED


@pytest.mark.unit
def test_compensate_mixed() -> None:
    """混合场景：部分有补偿、部分无补偿。"""
    executor = CompensationExecutor()

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"undone": step.id}

    executor.registry.register("a", handler)
    # b 和 c 无补偿 handler。

    defn = _make_definition()
    results = asyncio.run(executor.compensate(defn, ["a", "b", "c"], {}))

    assert results["a"] == StepStatus.COMPENSATED
    assert results["b"] == StepStatus.SKIPPED
    assert results["c"] == StepStatus.SKIPPED


@pytest.mark.unit
def test_compensate_unknown_step_skipped() -> None:
    """不在定义中的步骤标记为 SKIPPED。"""
    executor = CompensationExecutor()
    defn = WorkflowDefinition(name="flow", steps=[Step(id="a", uses="x")])

    results = asyncio.run(executor.compensate(defn, ["a", "ghost"], {}))

    assert results["a"] == StepStatus.SKIPPED
    assert results["ghost"] == StepStatus.SKIPPED


# --------------------------------------------------------------------------- #
# 空列表
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_compensate_empty() -> None:
    """空已完成列表返回空结果。"""
    executor = CompensationExecutor()
    defn = _make_definition()

    results = asyncio.run(executor.compensate(defn, [], {}))
    assert results == {}
