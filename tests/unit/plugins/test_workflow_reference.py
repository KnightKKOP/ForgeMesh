"""参考工作流运行时插件单元测试（对齐 development-plan 5.4 / ADR-003）。

覆盖：
* start 创建运行实例；
* 幂等（同 key 返回已有）；
* get_status 查询；
* cancel（无补偿）；
* cancel（有补偿）；
* signal 审批通过/拒绝。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.core.errors import NotFoundError
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.approval import ApprovalGate
from forgemesh.workflowruntime.compensate import CompensationExecutor
from forgemesh.workflowruntime.exec import WorkflowExecutor
from forgemesh.workflowruntime.store import WorkflowStore
from workflow_reference_provider import ReferenceWorkflowRuntime


def _make_definition(
    steps: list[Step] | None = None,
) -> WorkflowDefinition:
    """构造测试用工作流定义。"""
    return WorkflowDefinition(
        name="test-flow",
        timeout_seconds=300,
        steps=steps or [Step(id="s1", uses="test.action")],
    )


async def _ok_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
    """始终成功的 handler。"""
    return {"step_id": step.id, "status": "ok"}


def _make_runtime(
    handler=None,
) -> ReferenceWorkflowRuntime:
    """构造测试用 ReferenceWorkflowRuntime。"""
    store = WorkflowStore()
    executor = WorkflowExecutor()
    compensation = CompensationExecutor()
    approval = ApprovalGate()
    return ReferenceWorkflowRuntime(
        store=store,
        executor=executor,
        compensation=compensation,
        approval=approval,
        handler=handler or _ok_handler,
    )


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_start_creates_run() -> None:
    """start 创建运行实例并返回 RunRef。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {"model": "a"}, "key-1"))

    assert ref.run_id is not None
    assert ref.definition_version > 0


@pytest.mark.unit
def test_start_idempotent() -> None:
    """同一 idempotency_key 返回同一 run_id。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref1 = asyncio.run(rt.start(defn, {}, "key-idem"))
    ref2 = asyncio.run(rt.start(defn, {}, "key-idem"))

    assert ref1.run_id == ref2.run_id


@pytest.mark.unit
def test_start_different_keys() -> None:
    """不同 key 创建不同运行实例。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref1 = asyncio.run(rt.start(defn, {}, "key-a"))
    ref2 = asyncio.run(rt.start(defn, {}, "key-b"))

    assert ref1.run_id != ref2.run_id


# --------------------------------------------------------------------------- #
# get_status
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_get_status() -> None:
    """start 后状态为 Running。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-status"))
    status = asyncio.run(rt.get_status(ref.run_id))

    assert status == WorkflowStatus.RUNNING


@pytest.mark.unit
def test_get_status_not_found() -> None:
    """查询不存在的 run_id 抛 NotFoundError。"""
    rt = _make_runtime()
    with pytest.raises(NotFoundError):
        asyncio.run(rt.get_status("nonexistent"))


# --------------------------------------------------------------------------- #
# cancel（无补偿）
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_cancel_without_cleanup() -> None:
    """cancel(cleanup=False) 仅更新状态为 Cancelled。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-cancel"))
    asyncio.run(rt.cancel(ref.run_id, cleanup=False))

    status = asyncio.run(rt.get_status(ref.run_id))
    assert status == WorkflowStatus.CANCELLED


# --------------------------------------------------------------------------- #
# cancel（有补偿）
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_cancel_with_cleanup() -> None:
    """cancel(cleanup=True) 执行补偿后更新状态为 Cancelled。"""
    store = WorkflowStore()
    executor = WorkflowExecutor()
    compensation = CompensationExecutor()
    approval = ApprovalGate()

    # 注册补偿 handler。
    async def comp_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"undone": step.id}

    compensation.registry.register("s1", comp_handler)

    rt = ReferenceWorkflowRuntime(
        store=store,
        executor=executor,
        compensation=compensation,
        approval=approval,
        handler=_ok_handler,
    )
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-cleanup"))

    # 模拟步骤已成功完成。
    asyncio.run(store.update_step_status(ref.run_id, "s1", StepStatus.RUNNING.value))
    asyncio.run(store.update_step_status(ref.run_id, "s1", StepStatus.SUCCEEDED.value, result={"ok": True}))

    asyncio.run(rt.cancel(ref.run_id, cleanup=True))

    status = asyncio.run(rt.get_status(ref.run_id))
    assert status == WorkflowStatus.CANCELLED


@pytest.mark.unit
def test_cancel_with_cleanup_no_completed_steps() -> None:
    """cancel(cleanup=True) 无已完成步骤时仍正常取消。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-cleanup-empty"))
    asyncio.run(rt.cancel(ref.run_id, cleanup=True))

    status = asyncio.run(rt.get_status(ref.run_id))
    assert status == WorkflowStatus.CANCELLED


# --------------------------------------------------------------------------- #
# signal 审批
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_signal_approve() -> None:
    """signal approve 发送审批通过信号。"""
    store = WorkflowStore()
    executor = WorkflowExecutor()
    compensation = CompensationExecutor()
    approval = ApprovalGate()

    rt = ReferenceWorkflowRuntime(
        store=store, executor=executor, compensation=compensation,
        approval=approval, handler=_ok_handler,
    )
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-approve"))

    asyncio.run(rt.signal(ref.run_id, "approve", {"step_id": "s1"}))

    # 审批已通过，is_pending 为 False。
    assert not approval.is_pending(ref.run_id, "s1")


@pytest.mark.unit
def test_signal_reject() -> None:
    """signal reject 发送审批拒绝信号。"""
    store = WorkflowStore()
    executor = WorkflowExecutor()
    compensation = CompensationExecutor()
    approval = ApprovalGate()

    rt = ReferenceWorkflowRuntime(
        store=store, executor=executor, compensation=compensation,
        approval=approval, handler=_ok_handler,
    )
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-reject"))

    asyncio.run(rt.signal(ref.run_id, "reject", {"step_id": "s1"}))

    assert not approval.is_pending(ref.run_id, "s1")


@pytest.mark.unit
def test_signal_missing_step_id() -> None:
    """signal 缺少 step_id 抛 ValueError。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-signal"))

    with pytest.raises(ValueError, match="step_id"):
        asyncio.run(rt.signal(ref.run_id, "approve", {}))


@pytest.mark.unit
def test_signal_unsupported_name() -> None:
    """不支持的信号名称抛 ValueError。"""
    rt = _make_runtime()
    defn = _make_definition()

    ref = asyncio.run(rt.start(defn, {}, "key-signal-unsupported"))

    with pytest.raises(ValueError, match="unsupported signal"):
        asyncio.run(rt.signal(ref.run_id, "unknown", {"step_id": "s1"}))


@pytest.mark.unit
def test_signal_run_not_found() -> None:
    """对不存在的 run 投 NotFoundError。"""
    rt = _make_runtime()
    with pytest.raises(NotFoundError):
        asyncio.run(rt.signal("no-such-run", "approve", {"step_id": "s1"}))
