"""workflow-temporal 插件单元测试（对齐 development-plan 5.4 / ADR-003）。

覆盖：

* **start** — 创建运行实例 / 幂等 / 不同 key 创建不同实例。
* **get_status** — 查询状态 / 不存在抛 NotFoundError / 状态映射。
* **cancel** — 无补偿取消 / 有补偿取消 / 补偿逆序执行。
* **signal** — 信号投递 / 不存在抛 NotFoundError。
* **register_compensation** — 注册补偿函数。
* **temporal_eval_report** — 适配评估报告。
* **语义不变性** — 与参考执行器（workflow-reference）行为一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.core.errors import NotFoundError
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import WorkflowStatus
from workflow_temporal_plugin.provider import (
    InMemoryTemporalClient,
    TemporalRunStatus,
    TemporalWorkflowProvider,
    temporal_eval_report,
)


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """为每个测试提供干净的 current event loop（兼容 3.9–3.12）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(coro: Any) -> Any:
    """在当前事件循环上运行协程并返回结果。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_definition(steps: list[Step] | None = None) -> WorkflowDefinition:
    """构造测试用工作流定义。"""
    return WorkflowDefinition(
        name="test-flow",
        timeout_seconds=300,
        steps=steps or [Step(id="s1", uses="test.action")],
    )


def _make_provider(
    client: InMemoryTemporalClient | None = None,
) -> TemporalWorkflowProvider:
    """构造测试用 TemporalWorkflowProvider。"""
    return TemporalWorkflowProvider(client=client or InMemoryTemporalClient())


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_start_creates_run() -> None:
    """start 创建运行实例并返回 RunRef。"""
    provider = _make_provider()
    defn = _make_definition()

    ref = _run(provider.start(defn, {"model": "a"}, "key-1"))

    assert ref.run_id is not None
    assert ref.definition_version > 0


@pytest.mark.unit
def test_start_idempotent() -> None:
    """同一 idempotency_key 返回同一 run_id（幂等语义）。"""
    provider = _make_provider()
    defn = _make_definition()

    ref1 = _run(provider.start(defn, {}, "key-idem"))
    ref2 = _run(provider.start(defn, {}, "key-idem"))

    assert ref1.run_id == ref2.run_id


@pytest.mark.unit
def test_start_different_keys() -> None:
    """不同 key 创建不同运行实例。"""
    provider = _make_provider()
    defn = _make_definition()

    ref1 = _run(provider.start(defn, {}, "key-a"))
    ref2 = _run(provider.start(defn, {}, "key-b"))

    assert ref1.run_id != ref2.run_id


# --------------------------------------------------------------------------- #
# get_status
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_get_status_running_after_start() -> None:
    """start 后状态为 Running（Temporal RUNNING → WorkflowStatus.RUNNING）。"""
    provider = _make_provider()
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-status"))
    status = _run(provider.get_status(ref.run_id))

    assert status == WorkflowStatus.RUNNING


@pytest.mark.unit
def test_get_status_not_found() -> None:
    """查询不存在的 run_id 抛 NotFoundError。"""
    provider = _make_provider()
    with pytest.raises(NotFoundError):
        _run(provider.get_status("nonexistent"))


@pytest.mark.unit
def test_get_status_maps_completed_to_succeeded() -> None:
    """Temporal COMPLETED 映射为 WorkflowStatus.SUCCEEDED。"""
    client = InMemoryTemporalClient()
    provider = _make_provider(client=client)
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-done"))
    client.set_status(ref.run_id, TemporalRunStatus.COMPLETED)
    status = _run(provider.get_status(ref.run_id))

    assert status == WorkflowStatus.SUCCEEDED


@pytest.mark.unit
def test_get_status_maps_waiting() -> None:
    """Temporal WAITING 映射为 WorkflowStatus.WAITING（审批语义）。"""
    client = InMemoryTemporalClient()
    provider = _make_provider(client=client)
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-wait"))
    client.set_status(ref.run_id, TemporalRunStatus.WAITING)
    status = _run(provider.get_status(ref.run_id))

    assert status == WorkflowStatus.WAITING


@pytest.mark.unit
def test_get_status_maps_timed_out() -> None:
    """Temporal TIMED_OUT 映射为 WorkflowStatus.TIMEDOUT。"""
    client = InMemoryTemporalClient()
    provider = _make_provider(client=client)
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-timeout"))
    client.set_status(ref.run_id, TemporalRunStatus.TIMED_OUT)
    status = _run(provider.get_status(ref.run_id))

    assert status == WorkflowStatus.TIMEDOUT


# --------------------------------------------------------------------------- #
# cancel（无补偿）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_cancel_without_cleanup() -> None:
    """cancel(cleanup=False) 仅更新状态为 Cancelled。"""
    provider = _make_provider()
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-cancel"))
    _run(provider.cancel(ref.run_id, cleanup=False))

    status = _run(provider.get_status(ref.run_id))
    assert status == WorkflowStatus.CANCELLED


@pytest.mark.unit
def test_cancel_not_found() -> None:
    """取消不存在的 run_id 抛 NotFoundError。"""
    provider = _make_provider()
    with pytest.raises(NotFoundError):
        _run(provider.cancel("nonexistent"))


# --------------------------------------------------------------------------- #
# cancel（有补偿）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_cancel_with_cleanup_executes_compensation() -> None:
    """cancel(cleanup=True) 执行已注册补偿后更新状态为 Cancelled。"""
    provider = _make_provider()
    defn = _make_definition(steps=[Step(id="s1", uses="test.action")])

    # 注册补偿函数。
    executed: list[str] = []

    async def comp_fn(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        executed.append(step.id)
        return {"undone": step.id}

    provider.register_compensation("s1", comp_fn)

    ref = _run(provider.start(defn, {}, "key-cleanup"))
    _run(provider.cancel(ref.run_id, cleanup=True))

    status = _run(provider.get_status(ref.run_id))
    assert status == WorkflowStatus.CANCELLED
    assert executed == ["s1"]
    assert provider.compensation_log == ["s1"]


@pytest.mark.unit
def test_cancel_with_cleanup_runs_compensation_in_reverse_order() -> None:
    """cancel(cleanup=True) 按定义逆序执行补偿（反向回滚）。"""
    provider = _make_provider()
    defn = _make_definition(
        steps=[
            Step(id="s1", uses="test.action"),
            Step(id="s2", uses="test.action", needs=["s1"]),
            Step(id="s3", uses="test.action", needs=["s2"]),
        ],
    )

    executed: list[str] = []

    async def comp_fn(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        executed.append(step.id)
        return {"undone": step.id}

    provider.register_compensation("s1", comp_fn)
    provider.register_compensation("s2", comp_fn)
    provider.register_compensation("s3", comp_fn)

    ref = _run(provider.start(defn, {}, "key-reverse"))
    _run(provider.cancel(ref.run_id, cleanup=True))

    # 逆序执行：s3 → s2 → s1。
    assert executed == ["s3", "s2", "s1"]


@pytest.mark.unit
def test_cancel_with_cleanup_no_registered_compensation() -> None:
    """cancel(cleanup=True) 无已注册补偿时仍正常取消。"""
    provider = _make_provider()
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-cleanup-empty"))
    _run(provider.cancel(ref.run_id, cleanup=True))

    status = _run(provider.get_status(ref.run_id))
    assert status == WorkflowStatus.CANCELLED
    assert provider.compensation_log == []


# --------------------------------------------------------------------------- #
# signal
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_signal_delivers_to_client() -> None:
    """signal 将信号投递到 Temporal 客户端。"""
    client = InMemoryTemporalClient()
    provider = _make_provider(client=client)
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-signal"))
    _run(provider.signal(ref.run_id, "approve", {"step_id": "s1"}))

    assert len(client.signals) == 1
    wf_id, signal_name, payload = client.signals[0]
    assert wf_id == ref.run_id
    assert signal_name == "approve"
    assert payload == {"step_id": "s1"}


@pytest.mark.unit
def test_signal_not_found() -> None:
    """对不存在的 run 投 NotFoundError。"""
    provider = _make_provider()
    with pytest.raises(NotFoundError):
        _run(provider.signal("no-such-run", "approve", {"step_id": "s1"}))


# --------------------------------------------------------------------------- #
# register_compensation
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_register_compensation_stores_function() -> None:
    """register_compensation 应存储补偿函数，供 cancel(cleanup=True) 调用。"""
    provider = _make_provider()
    defn = _make_definition()

    async def comp_fn(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"undone": step.id}

    provider.register_compensation("s1", comp_fn)

    ref = _run(provider.start(defn, {}, "key-reg"))
    _run(provider.cancel(ref.run_id, cleanup=True))

    assert provider.compensation_log == ["s1"]


# --------------------------------------------------------------------------- #
# temporal_eval_report
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_temporal_eval_report_returns_valid_report() -> None:
    """temporal_eval_report 返回 ADR-003 适配评估报告。"""
    report = temporal_eval_report()

    assert report["adr"] == "ADR-003"
    assert report["conclusion"] == "feasible"
    assert report["engine"] == "temporal"
    assert report["contract"] == "WorkflowRuntime"

    # 所有语义维度均支持。
    semantics = report["semantics"]
    for dimension in (
        "persistence",
        "idempotency",
        "retry",
        "timeout",
        "cancel",
        "compensation",
        "approval",
        "version_pinning",
    ):
        assert semantics[dimension]["supported"] is True

    # 状态映射表包含全部 6 个 Temporal 状态。
    assert len(report["status_mapping"]) == 6


# --------------------------------------------------------------------------- #
# 语义不变性：与参考执行器（workflow-reference）行为一致
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_invariant_start_then_status_is_running() -> None:
    """语义不变性：start 后状态为 Running，与参考执行器一致。"""
    provider = _make_provider()
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-inv-1"))
    status = _run(provider.get_status(ref.run_id))

    # 与 workflow-reference 的 test_get_status 行为一致。
    assert status == WorkflowStatus.RUNNING


@pytest.mark.unit
def test_invariant_cancel_then_status_is_cancelled() -> None:
    """语义不变性：cancel 后状态为 Cancelled，与参考执行器一致。"""
    provider = _make_provider()
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-inv-2"))
    _run(provider.cancel(ref.run_id, cleanup=False))

    status = _run(provider.get_status(ref.run_id))
    # 与 workflow-reference 的 test_cancel_without_cleanup 行为一致。
    assert status == WorkflowStatus.CANCELLED


@pytest.mark.unit
def test_invariant_idempotent_start() -> None:
    """语义不变性：同一 idempotency_key 幂等，与参考执行器一致。"""
    provider = _make_provider()
    defn = _make_definition()

    ref1 = _run(provider.start(defn, {}, "key-inv-idem"))
    ref2 = _run(provider.start(defn, {}, "key-inv-idem"))

    # 与 workflow-reference 的 test_start_idempotent 行为一致。
    assert ref1.run_id == ref2.run_id


@pytest.mark.unit
def test_invariant_cancel_with_cleanup_then_status_is_cancelled() -> None:
    """语义不变性：cancel(cleanup=True) 后状态仍为 Cancelled，与参考执行器一致。"""
    provider = _make_provider()
    defn = _make_definition()

    async def comp_fn(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"undone": step.id}

    provider.register_compensation("s1", comp_fn)

    ref = _run(provider.start(defn, {}, "key-inv-cleanup"))
    _run(provider.cancel(ref.run_id, cleanup=True))

    status = _run(provider.get_status(ref.run_id))
    # 与 workflow-reference 的 test_cancel_with_cleanup 行为一致。
    assert status == WorkflowStatus.CANCELLED


@pytest.mark.unit
def test_invariant_definition_version_pinned() -> None:
    """语义不变性：RunRef 固定 definition_version，升级定义不影响既有实例。"""
    provider = TemporalWorkflowProvider(definition_version=42)
    defn = _make_definition()

    ref = _run(provider.start(defn, {}, "key-inv-version"))

    # 版本固定语义（ADR-011）。
    assert ref.definition_version == 42
