"""参考工作流运行时插件单元测试（对齐 development-plan 5.4 / ADR-003）。

插件以子类提供 :class:`forgemesh.workflowruntime.runtime.LocalWorkflowRuntime`
（引擎的完整语义见 ``tests/unit/workflowruntime/test_runtime.py``）。覆盖：

* start 创建运行实例、后台执行到 Succeeded；
* 幂等（同 key 返回已有运行，不再执行一次）；
* get_status 查询；
* cancel（无补偿 / 有补偿）；
* signal 审批通过/拒绝唤醒等待中的步骤，载荷与信号名校验。

每个场景在一个事件循环里完成，结束前关闭运行时与存储。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from forgemesh.core.errors import NotFoundError
from forgemesh.workflow.runtime import RetryPolicy, Step, WorkflowDefinition
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.approval import ApprovalGate
from forgemesh.workflowruntime.compensate import CompensationExecutor
from forgemesh.workflowruntime.exec import StepExecutor, WorkflowExecutor
from forgemesh.workflowruntime.runtime import LocalWorkflowRuntime
from forgemesh.workflowruntime.store import WorkflowStore
from workflow_reference_provider import ReferenceWorkflowRuntime

pytestmark = pytest.mark.unit


def _make_definition(steps: list[Step] | None = None) -> WorkflowDefinition:
    """构造测试用工作流定义。"""
    return WorkflowDefinition(
        name="test-flow",
        timeout_seconds=300,
        steps=steps or [Step(id="s1", uses="test.action")],
    )


class _Handler:
    """记录调用的步骤 handler；``hold`` 时阻塞到 ``release`` 被 set。"""

    def __init__(self, hold: bool = False) -> None:
        self.calls: list[str] = []
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        if not hold:
            self.release.set()

    async def __call__(self, step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(step.id)
        self.entered.set()
        await self.release.wait()
        return {"step_id": step.id, "status": "ok"}


def _make_runtime(
    handler: Callable[[Step, dict[str, Any]], Awaitable[dict[str, Any]]],
    compensation: CompensationExecutor | None = None,
) -> tuple[ReferenceWorkflowRuntime, WorkflowStore]:
    store = WorkflowStore()
    runtime = ReferenceWorkflowRuntime(
        store=store,
        executor=WorkflowExecutor(StepExecutor(RetryPolicy(max_attempts=1))),
        compensation=compensation or CompensationExecutor(),
        approval=ApprovalGate(),
        handler=handler,
    )
    return runtime, store


async def _close(runtime: ReferenceWorkflowRuntime, store: WorkflowStore) -> None:
    await runtime.aclose()
    await store.close()


def test_reference_runtime_is_the_local_runtime() -> None:
    assert issubclass(ReferenceWorkflowRuntime, LocalWorkflowRuntime)


# --------------------------------------------------------------------------- #
# start / get_status
# --------------------------------------------------------------------------- #
def test_start_runs_the_workflow_in_the_background() -> None:
    """start 返回 RunRef 时运行为 Running，步骤在后台执行完后为 Succeeded。"""

    async def scenario() -> tuple[Any, ...]:
        handler = _Handler(hold=True)
        runtime, store = _make_runtime(handler)
        ref = await runtime.start(_make_definition(), {"model": "a"}, "key-1")
        await handler.entered.wait()
        running = await runtime.get_status(ref.run_id)
        handler.release.set()
        info = await runtime.wait(ref.run_id)
        await _close(runtime, store)
        return ref, running, info, handler.calls

    ref, running, info, calls = asyncio.run(scenario())

    assert ref.run_id
    assert ref.definition_version > 0
    assert running is WorkflowStatus.RUNNING
    assert info.status is WorkflowStatus.SUCCEEDED
    assert info.outputs == {"s1": {"step_id": "s1", "status": "ok"}}
    assert calls == ["s1"]


def test_start_is_idempotent_per_key() -> None:
    """同一 idempotency_key 返回同一运行且不再执行；不同 key 创建不同运行。"""

    async def scenario() -> tuple[str, str, str, list[str]]:
        handler = _Handler()
        runtime, store = _make_runtime(handler)
        first = await runtime.start(_make_definition(), {}, "key-idem")
        await runtime.wait(first.run_id)
        again = await runtime.start(_make_definition(), {}, "key-idem")
        await runtime.wait(again.run_id)
        other = await runtime.start(_make_definition(), {}, "key-other")
        await runtime.wait(other.run_id)
        await _close(runtime, store)
        return first.run_id, again.run_id, other.run_id, handler.calls

    first, again, other, calls = asyncio.run(scenario())

    assert first == again
    assert other != first
    assert calls == ["s1", "s1"]


def test_get_status_not_found() -> None:
    """查询不存在的 run_id 抛 NotFoundError。"""

    async def scenario() -> None:
        runtime, store = _make_runtime(_Handler())
        try:
            await runtime.get_status("nonexistent")
        finally:
            await _close(runtime, store)

    with pytest.raises(NotFoundError):
        asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #
def test_cancel_without_cleanup() -> None:
    """cancel(cleanup=False) 中断正在执行的步骤，运行进入 Cancelled。"""

    async def scenario() -> Any:
        handler = _Handler(hold=True)
        runtime, store = _make_runtime(handler)
        ref = await runtime.start(_make_definition(), {}, "key-cancel")
        await handler.entered.wait()
        await runtime.cancel(ref.run_id, cleanup=False)
        info = await runtime.get_run(ref.run_id)
        await _close(runtime, store)
        return info

    info = asyncio.run(scenario())

    assert info.status is WorkflowStatus.CANCELLED
    assert info.steps == {"s1": StepStatus.CANCELLED}


def test_cancel_with_cleanup_compensates_completed_steps() -> None:
    """cancel(cleanup=True) 先补偿已成功的步骤，再进入 Cancelled。"""
    undone: list[tuple[str, dict[str, Any]]] = []

    async def undo(step: Step, result: dict[str, Any]) -> dict[str, Any]:
        undone.append((step.id, result))
        return {"undone": step.id}

    async def scenario() -> Any:
        compensation = CompensationExecutor()
        compensation.registry.register("s1", undo)
        hold_s2 = asyncio.Event()

        async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
            if step.id == "s2":
                hold_s2.set()
                await asyncio.Event().wait()
            return {"ok": step.id}

        runtime, store = _make_runtime(handler, compensation)
        defn = _make_definition(
            [Step(id="s1", uses="test.action"), Step(id="s2", uses="test.action")]
        )
        ref = await runtime.start(defn, {}, "key-cleanup")
        await hold_s2.wait()
        await runtime.cancel(ref.run_id, cleanup=True)
        info = await runtime.get_run(ref.run_id)
        await _close(runtime, store)
        return info

    info = asyncio.run(scenario())

    assert info.status is WorkflowStatus.CANCELLED
    assert info.steps == {"s1": StepStatus.COMPENSATED, "s2": StepStatus.CANCELLED}
    assert undone == [("s1", {"ok": "s1"})]


# --------------------------------------------------------------------------- #
# signal 审批
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("signal", "expected"),
    [("approve", WorkflowStatus.SUCCEEDED), ("reject", WorkflowStatus.FAILED)],
)
def test_signal_wakes_the_waiting_step(signal: str, expected: WorkflowStatus) -> None:
    """requires_approval 的步骤等待审批；approve 继续执行，reject 使运行失败。"""

    async def scenario() -> tuple[WorkflowStatus, WorkflowStatus, list[str]]:
        handler = _Handler()
        runtime, store = _make_runtime(handler)
        defn = _make_definition([Step(id="s1", uses="test.action", requires_approval=True)])
        ref = await runtime.start(defn, {}, f"key-{signal}")
        async with asyncio.timeout(5):
            while (waiting := await runtime.get_status(ref.run_id)) is WorkflowStatus.RUNNING:
                await asyncio.sleep(0.001)
        await runtime.signal(ref.run_id, signal, {"step_id": "s1"})
        info = await runtime.wait(ref.run_id)
        await _close(runtime, store)
        return waiting, info.status, handler.calls

    waiting, final, calls = asyncio.run(scenario())

    assert waiting is WorkflowStatus.WAITING
    assert final is expected
    assert calls == (["s1"] if signal == "approve" else [])


@pytest.mark.parametrize(
    ("name", "payload", "match"),
    [
        ("approve", {}, "step_id"),
        ("unknown", {"step_id": "s1"}, "unsupported signal"),
    ],
)
def test_signal_validates_the_payload_and_name(
    name: str, payload: dict[str, Any], match: str
) -> None:
    async def scenario() -> None:
        runtime, store = _make_runtime(_Handler())
        ref = await runtime.start(_make_definition(), {}, "key-signal")
        try:
            await runtime.signal(ref.run_id, name, payload)
        finally:
            await _close(runtime, store)

    with pytest.raises(ValueError, match=match):
        asyncio.run(scenario())


def test_signal_run_not_found() -> None:
    """对不存在的 run 投递信号抛 NotFoundError。"""

    async def scenario() -> None:
        runtime, store = _make_runtime(_Handler())
        try:
            await runtime.signal("no-such-run", "approve", {"step_id": "s1"})
        finally:
            await _close(runtime, store)

    with pytest.raises(NotFoundError):
        asyncio.run(scenario())
