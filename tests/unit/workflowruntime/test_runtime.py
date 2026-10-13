"""LocalWorkflowRuntime 单元测试：后台执行、失败补偿、重启恢复、取消与关闭。

每个场景在一个事件循环里完成，结束前 ``aclose()`` 运行时并关闭存储；重启场景用
``tmp_path`` 下的 SQLite 文件，以"新运行时重开同一文件"模拟进程重启。
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

pytestmark = pytest.mark.unit

FAST = RetryPolicy(max_attempts=2, initial_backoff_seconds=0.0, max_backoff_seconds=0.0)

DEFN = WorkflowDefinition(
    name="deploy",
    steps=[
        Step(id="prepare", uses="test.prepare"),
        Step(id="launch", uses="test.launch"),
        Step(id="verify", uses="test.verify"),
    ],
)

Compensation = Callable[[Step, dict[str, Any]], Awaitable[dict[str, Any]]]


class _Steps:
    """可控的步骤 handler：记录调用与输入，可按步骤阻塞或失败。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.inputs: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        #: 步骤 id → 剩余失败次数（``None`` 表示一直失败）。
        self.failures: dict[str, int | None] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}

    def block(self, step_id: str) -> asyncio.Event:
        """让步骤阻塞到返回的事件被 set，并可经 ``entered[step_id]`` 等它开始。"""
        self.entered[step_id] = asyncio.Event()
        self.gates[step_id] = asyncio.Event()
        return self.gates[step_id]

    async def handle(self, step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(step.id)
        self.inputs[step.id] = dict(inputs)
        if step.id in self.entered:
            self.entered[step.id].set()
        if step.id in self.gates:
            try:
                await self.gates[step.id].wait()
            except asyncio.CancelledError:
                self.cancelled.append(step.id)
                raise
        if step.id in self.failures:
            remaining = self.failures[step.id]
            if remaining is None or remaining > 0:
                if remaining is not None:
                    self.failures[step.id] = remaining - 1
                raise RuntimeError(f"{step.id} broke")
        return {"done": step.id}


class _Compensations:
    """记录补偿调用（步骤 id 与收到的步骤结果）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, *, fail: bool = False, gate: asyncio.Event | None = None) -> Compensation:
        async def compensate(step: Step, result: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((step.id, result))
            if gate is not None:
                await gate.wait()
            if fail:
                raise RuntimeError(f"undo {step.id} broke")
            return {}

        return compensate


def _runtime(
    store: WorkflowStore,
    steps: _Steps,
    compensations: dict[str, Compensation] | None = None,
    approval: ApprovalGate | None = None,
) -> LocalWorkflowRuntime:
    compensation = CompensationExecutor()
    for step_id, handler in (compensations or {}).items():
        compensation.registry.register(step_id, handler)
    return LocalWorkflowRuntime(
        store,
        WorkflowExecutor(StepExecutor(FAST)),
        compensation,
        approval or ApprovalGate(),
        steps.handle,
    )


async def _until(runtime: LocalWorkflowRuntime, run_id: str, status: WorkflowStatus) -> None:
    async with asyncio.timeout(5):
        while await runtime.get_status(run_id) is not status:
            await asyncio.sleep(0.001)


def test_start_executes_the_steps_in_the_background() -> None:
    async def scenario() -> tuple[Any, ...]:
        store = WorkflowStore()
        steps = _Steps()
        gate = steps.block("prepare")
        runtime = _runtime(store, steps)
        ref = await runtime.start(DEFN, {"model": "m"}, "uid-1:1")
        await steps.entered["prepare"].wait()
        while_running = await runtime.get_run(ref.run_id)
        gate.set()
        info = await runtime.wait(ref.run_id)
        again = await runtime.start(DEFN, {"model": "m"}, "uid-1:1")
        await runtime.wait(ref.run_id)
        found = await runtime.find_run("uid-1:1")
        missing = await runtime.find_run("uid-2:1")
        await runtime.aclose()
        await store.close()
        return ref, while_running, info, again, found, missing, steps

    ref, while_running, info, again, found, missing, steps = asyncio.run(scenario())

    assert while_running.status is WorkflowStatus.RUNNING
    assert while_running.steps["prepare"] is StepStatus.RUNNING
    assert info.status is WorkflowStatus.SUCCEEDED
    assert info.terminal
    assert info.steps == {
        "prepare": StepStatus.SUCCEEDED,
        "launch": StepStatus.SUCCEEDED,
        "verify": StepStatus.SUCCEEDED,
    }
    assert info.outputs == {step: {"done": step} for step in ("prepare", "launch", "verify")}
    assert info.error is None
    assert steps.inputs["verify"] == {
        "model": "m",
        "prepare": {"done": "prepare"},
        "launch": {"done": "launch"},
    }
    assert again.run_id == ref.run_id
    assert steps.calls == ["prepare", "launch", "verify"]
    assert found == info
    assert missing is None


def test_find_runs_returns_the_snapshots_by_key_in_creation_order() -> None:
    async def scenario() -> tuple[Any, ...]:
        store = WorkflowStore()
        steps = _Steps()
        runtime = _runtime(store, steps)
        second = await runtime.start(DEFN, {}, "uid-1:2")
        first = await runtime.start(DEFN, {}, "uid-1:1")
        await runtime.start(DEFN, {}, "uid-2:1")
        await runtime.wait(second.run_id)
        await runtime.wait(first.run_id)
        found = await runtime.find_runs("uid-1:")
        snapshots = [await runtime.get_run(ref.run_id) for ref in (second, first)]
        missing = await runtime.find_runs("uid-3:")
        await runtime.aclose()
        await store.close()
        return found, snapshots, missing

    found, snapshots, missing = asyncio.run(scenario())

    assert list(found) == ["uid-1:2", "uid-1:1"]
    assert list(found.values()) == snapshots
    assert all(info.status is WorkflowStatus.SUCCEEDED for info in found.values())
    assert missing == {}


def test_concurrent_starts_with_one_key_execute_one_run() -> None:
    async def scenario() -> tuple[set[str], list[str]]:
        store = WorkflowStore()
        steps = _Steps()
        runtime = _runtime(store, steps)
        refs = await asyncio.gather(*(runtime.start(DEFN, {}, "same-key") for _ in range(5)))
        await runtime.wait(refs[0].run_id)
        await runtime.aclose()
        await store.close()
        return {ref.run_id for ref in refs}, steps.calls

    run_ids, calls = asyncio.run(scenario())

    assert len(run_ids) == 1
    assert calls == ["prepare", "launch", "verify"]


def test_a_failed_attempt_is_retried_by_the_retry_policy() -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        store = WorkflowStore()
        steps = _Steps()
        steps.failures["launch"] = 1
        runtime = _runtime(store, steps)
        ref = await runtime.start(DEFN, {}, "key")
        info = await runtime.wait(ref.run_id)
        row = await store.get_run(ref.run_id)
        await runtime.aclose()
        await store.close()
        return info, row

    info, row = asyncio.run(scenario())

    assert info.status is WorkflowStatus.SUCCEEDED
    launch = next(step for step in row["steps"] if step["step_id"] == "launch")
    assert launch["attempt_count"] == 2
    assert launch["result"] == {"done": "launch"}


def test_a_failing_step_compensates_the_completed_steps_in_reverse() -> None:
    async def scenario() -> tuple[Any, _Steps, _Compensations]:
        store = WorkflowStore()
        steps = _Steps()
        steps.failures["verify"] = None
        undo = _Compensations()
        runtime = _runtime(
            store,
            steps,
            {"prepare": undo.handler(), "launch": undo.handler(), "verify": undo.handler()},
        )
        ref = await runtime.start(DEFN, {}, "key")
        info = await runtime.wait(ref.run_id)
        await runtime.aclose()
        await store.close()
        return info, steps, undo

    info, steps, undo = asyncio.run(scenario())

    assert info.status is WorkflowStatus.FAILED
    assert info.error == "step 'verify' failed: verify broke"
    assert info.steps == {
        "prepare": StepStatus.COMPENSATED,
        "launch": StepStatus.COMPENSATED,
        "verify": StepStatus.FAILED,
    }
    assert undo.calls == [("launch", {"done": "launch"}), ("prepare", {"done": "prepare"})]
    assert steps.calls == ["prepare", "launch", "verify", "verify"]
    assert info.outputs == {"prepare": {"done": "prepare"}, "launch": {"done": "launch"}}


def test_steps_after_the_failure_are_skipped_and_failed_compensations_are_recorded() -> None:
    async def scenario() -> tuple[Any, _Steps, _Compensations]:
        store = WorkflowStore()
        steps = _Steps()
        steps.failures["launch"] = None
        undo = _Compensations()
        runtime = _runtime(store, steps, {"prepare": undo.handler(fail=True)})
        ref = await runtime.start(DEFN, {}, "key")
        info = await runtime.wait(ref.run_id)
        await runtime.aclose()
        await store.close()
        return info, steps, undo

    info, steps, undo = asyncio.run(scenario())

    assert info.status is WorkflowStatus.FAILED
    assert info.steps == {
        "prepare": StepStatus.COMPENSATION_FAILED,
        "launch": StepStatus.FAILED,
        "verify": StepStatus.SKIPPED,
    }
    assert "verify" not in steps.calls
    assert undo.calls == [("prepare", {"done": "prepare"})]


def test_the_workflow_timeout_fails_the_run_instead_of_leaving_it_running() -> None:
    defn = DEFN.model_copy(update={"timeout_seconds": 1})

    async def scenario() -> Any:
        store = WorkflowStore()
        steps = _Steps()
        steps.block("launch")
        runtime = _runtime(store, steps)
        ref = await runtime.start(defn, {}, "key")
        info = await runtime.wait(ref.run_id)
        await runtime.aclose()
        await store.close()
        return info

    info = asyncio.run(scenario())

    assert info.status is WorkflowStatus.FAILED
    assert info.error == "step 'launch' failed: workflow 'deploy' timed out after 1s"
    assert info.steps["launch"] is StepStatus.FAILED
    assert info.steps["verify"] is StepStatus.SKIPPED


def test_recover_resumes_a_run_interrupted_in_the_middle_of_a_step(tmp_path) -> None:
    db_path = str(tmp_path / "workflows.db")

    async def before_restart() -> tuple[str, WorkflowStatus, _Steps]:
        store = WorkflowStore(db_path)
        steps = _Steps()
        steps.block("launch")
        runtime = _runtime(store, steps)
        ref = await runtime.start(DEFN, {"model": "m"}, "key")
        await steps.entered["launch"].wait()
        await runtime.aclose()
        status = await runtime.get_status(ref.run_id)
        await store.close()
        return ref.run_id, status, steps

    run_id, status, steps = asyncio.run(before_restart())

    assert status is WorkflowStatus.RUNNING
    assert steps.calls == ["prepare", "launch"]
    assert steps.cancelled == ["launch"]

    async def after_restart() -> tuple[list[str], Any, dict[str, Any], _Steps]:
        store = WorkflowStore(db_path)
        steps = _Steps()
        runtime = _runtime(store, steps)
        resumed = await runtime.recover()
        info = await runtime.wait(run_id)
        row = await store.get_run(run_id)
        await runtime.aclose()
        await store.close()
        return resumed, info, row, steps

    resumed, info, row, steps = asyncio.run(after_restart())

    assert resumed == [run_id]
    assert info.status is WorkflowStatus.SUCCEEDED
    assert steps.calls == ["launch", "verify"]
    assert steps.inputs["launch"] == {"model": "m", "prepare": {"done": "prepare"}}
    attempts = {step["step_id"]: step["attempt_count"] for step in row["steps"]}
    assert attempts == {"prepare": 1, "launch": 2, "verify": 1}


def test_recover_finishes_a_compensation_interrupted_by_a_restart(tmp_path) -> None:
    db_path = str(tmp_path / "workflows.db")

    async def before_restart() -> tuple[str, Any, _Compensations]:
        store = WorkflowStore(db_path)
        steps = _Steps()
        steps.failures["verify"] = None
        undo = _Compensations()
        stuck = asyncio.Event()
        runtime = _runtime(store, steps, {"launch": undo.handler(gate=stuck)})
        ref = await runtime.start(DEFN, {}, "key")
        await _until(runtime, ref.run_id, WorkflowStatus.COMPENSATING)
        while not undo.calls:
            await asyncio.sleep(0.001)
        await runtime.aclose(timeout=0.01)
        info = await runtime.get_run(ref.run_id)
        await store.close()
        return ref.run_id, info, undo

    run_id, interrupted, undo = asyncio.run(before_restart())

    assert interrupted.status is WorkflowStatus.COMPENSATING
    assert interrupted.steps["launch"] is StepStatus.COMPENSATING
    assert undo.calls == [("launch", {"done": "launch"})]

    async def after_restart() -> tuple[Any, _Steps, _Compensations]:
        store = WorkflowStore(db_path)
        steps = _Steps()
        undo = _Compensations()
        runtime = _runtime(store, steps, {"launch": undo.handler(), "prepare": undo.handler()})
        await runtime.recover()
        info = await runtime.wait(run_id)
        await runtime.aclose()
        await store.close()
        return info, steps, undo

    info, steps, undo = asyncio.run(after_restart())

    assert info.status is WorkflowStatus.FAILED
    assert info.error == "step 'verify' failed: verify broke"
    assert info.steps == {
        "prepare": StepStatus.COMPENSATED,
        "launch": StepStatus.COMPENSATED,
        "verify": StepStatus.FAILED,
    }
    assert undo.calls == [("launch", {"done": "launch"}), ("prepare", {"done": "prepare"})]
    assert steps.calls == []


@pytest.mark.parametrize("cleanup", [True, False])
def test_cancel_interrupts_the_running_step(cleanup: bool) -> None:
    async def scenario() -> tuple[Any, _Steps, _Compensations]:
        store = WorkflowStore()
        steps = _Steps()
        steps.block("launch")
        undo = _Compensations()
        runtime = _runtime(store, steps, {"prepare": undo.handler(), "launch": undo.handler()})
        ref = await runtime.start(DEFN, {}, "key")
        await steps.entered["launch"].wait()
        await runtime.cancel(ref.run_id, cleanup=cleanup)
        info = await runtime.get_run(ref.run_id)
        await runtime.aclose()
        await store.close()
        return info, steps, undo

    info, steps, undo = asyncio.run(scenario())

    assert info.status is WorkflowStatus.CANCELLED
    assert steps.cancelled == ["launch"]
    assert info.steps["launch"] is StepStatus.CANCELLED
    assert info.steps["verify"] is StepStatus.CANCELLED
    if cleanup:
        assert info.steps["prepare"] is StepStatus.COMPENSATED
        assert undo.calls == [("prepare", {"done": "prepare"})]
    else:
        assert info.steps["prepare"] is StepStatus.SUCCEEDED
        assert undo.calls == []


def test_cancel_finishes_a_run_that_is_not_executing() -> None:
    async def scenario() -> tuple[Any, _Steps, _Compensations]:
        store = WorkflowStore()
        ref = await store.create_run(DEFN, {}, "key")
        await store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value)
        await store.update_step_status(ref.run_id, "prepare", StepStatus.RUNNING.value)
        await store.update_step_status(
            ref.run_id, "prepare", StepStatus.SUCCEEDED.value, {"done": "prepare"}
        )
        steps = _Steps()
        undo = _Compensations()
        runtime = _runtime(store, steps, {"prepare": undo.handler()})
        await runtime.cancel(ref.run_id, cleanup=True)
        info = await runtime.get_run(ref.run_id)
        await runtime.aclose()
        await store.close()
        return info, steps, undo

    info, steps, undo = asyncio.run(scenario())

    assert info.status is WorkflowStatus.CANCELLED
    assert info.steps == {
        "prepare": StepStatus.COMPENSATED,
        "launch": StepStatus.CANCELLED,
        "verify": StepStatus.CANCELLED,
    }
    assert steps.calls == []
    assert undo.calls == [("prepare", {"done": "prepare"})]


def test_cancel_leaves_finished_runs_alone_and_rejects_unknown_runs() -> None:
    async def scenario() -> tuple[Any, _Compensations]:
        store = WorkflowStore()
        undo = _Compensations()
        runtime = _runtime(store, _Steps(), {"prepare": undo.handler()})
        ref = await runtime.start(DEFN, {}, "key")
        await runtime.wait(ref.run_id)
        await runtime.cancel(ref.run_id, cleanup=True)
        info = await runtime.get_run(ref.run_id)
        with pytest.raises(NotFoundError):
            await runtime.cancel("run-missing")
        await runtime.aclose()
        await store.close()
        return info, undo

    info, undo = asyncio.run(scenario())

    assert info.status is WorkflowStatus.SUCCEEDED
    assert undo.calls == []


def test_aclose_stops_every_task_and_refuses_new_work() -> None:
    async def scenario() -> tuple[WorkflowStatus, set[asyncio.Task[Any]]]:
        store = WorkflowStore()
        steps = _Steps()
        steps.block("prepare")
        runtime = _runtime(store, steps)
        ref = await runtime.start(DEFN, {}, "key")
        await steps.entered["prepare"].wait()
        await runtime.aclose()
        leftover = asyncio.all_tasks() - {asyncio.current_task()}
        with pytest.raises(RuntimeError, match="closed"):
            await runtime.start(DEFN, {}, "other-key")
        with pytest.raises(RuntimeError, match="closed"):
            await runtime.cancel(ref.run_id)
        with pytest.raises(RuntimeError, match="closed"):
            await runtime.recover()
        status = await runtime.get_status(ref.run_id)
        await store.close()
        return status, leftover

    status, leftover = asyncio.run(scenario())

    assert status is WorkflowStatus.RUNNING
    assert leftover == set()


def test_an_approval_step_waits_for_the_signal() -> None:
    defn = DEFN.model_copy(
        update={
            "steps": [
                Step(id="prepare", uses="test.prepare"),
                Step(id="launch", uses="test.launch", requires_approval=True),
            ]
        }
    )

    async def scenario() -> tuple[list[str], Any]:
        store = WorkflowStore()
        steps = _Steps()
        runtime = _runtime(store, steps)
        ref = await runtime.start(defn, {}, "key")
        await _until(runtime, ref.run_id, WorkflowStatus.WAITING)
        calls_while_waiting = list(steps.calls)
        await runtime.signal(ref.run_id, "approve", {"step_id": "launch"})
        info = await runtime.wait(ref.run_id)
        await runtime.aclose()
        await store.close()
        return calls_while_waiting, info

    calls_while_waiting, info = asyncio.run(scenario())

    assert calls_while_waiting == ["prepare"]
    assert info.status is WorkflowStatus.SUCCEEDED
    assert info.outputs["launch"] == {"done": "launch"}


def test_a_rejected_approval_fails_the_run() -> None:
    defn = DEFN.model_copy(
        update={
            "steps": [
                Step(id="prepare", uses="test.prepare"),
                Step(id="launch", uses="test.launch", requires_approval=True),
            ]
        }
    )

    async def scenario() -> tuple[Any, _Steps, _Compensations]:
        store = WorkflowStore()
        steps = _Steps()
        undo = _Compensations()
        runtime = _runtime(store, steps, {"prepare": undo.handler()})
        ref = await runtime.start(defn, {}, "key")
        await _until(runtime, ref.run_id, WorkflowStatus.WAITING)
        await runtime.signal(ref.run_id, "reject", {"step_id": "launch"})
        info = await runtime.wait(ref.run_id)
        await runtime.aclose()
        await store.close()
        return info, steps, undo

    info, steps, undo = asyncio.run(scenario())

    assert info.status is WorkflowStatus.FAILED
    assert info.error == "step 'launch' failed: approval of step 'launch' was rejected"
    assert info.steps == {"prepare": StepStatus.COMPENSATED, "launch": StepStatus.SKIPPED}
    assert steps.calls == ["prepare"]
    assert undo.calls == [("prepare", {"done": "prepare"})]
