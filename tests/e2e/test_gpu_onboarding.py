"""GPU 上线端到端工作流 E2E 测试（对齐 development-plan WP-3.5）。

验证完整闭环 discover → validate → prepare → deploy → verify → commit：

* 工作流定义有效（6 步、DAG 无环、依赖正确）；
* 步骤顺序正确；
* 完整闭环执行成功；
* 各步骤失败时工作流终止（后续步骤不执行）；
* WorkflowExecutor 集成执行；
* 持久化运行实例 + 恢复。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.workflow.runtime import RetryPolicy, Step
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.dsl import validate_dag
from forgemesh.workflowruntime.exec import StepExecutor, WorkflowExecutor
from forgemesh.workflowruntime.gpu_onboarding import GPUOnboardingWorkflow
from forgemesh.workflowruntime.store import WorkflowStore

# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


class MockOnboardingHandler:
    """模拟 GPU 上线各步骤执行的 handler。

    记录已执行步骤顺序，并可在指定步骤抛异常以模拟失败。

    Args:
        fail_at: 触发失败的步骤 id；``None`` 表示全部成功。
    """

    def __init__(self, fail_at: str | None = None) -> None:
        self.executed: list[str] = []
        self.fail_at = fail_at

    async def __call__(self, step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        self.executed.append(step.id)
        if self.fail_at is not None and step.id == self.fail_at:
            raise RuntimeError(f"step '{step.id}' failed (mock)")
        return {"step_id": step.id, "status": "succeeded"}


def _no_retry_executor() -> WorkflowExecutor:
    """创建不重试的 WorkflowExecutor（避免失败测试中的退避延迟）。"""
    return WorkflowExecutor(StepExecutor(RetryPolicy(max_attempts=1)))


def _default_inputs() -> dict[str, Any]:
    """构造默认工作流输入。"""
    return GPUOnboardingWorkflow().build_inputs(
        node_id="node-gpu-01",
        model_uri="s3://models/llama-7b",
        model_digest="sha256:abc123",
        gpu_ids=["gpu-0", "gpu-1"],
        port=8000,
    )


# --------------------------------------------------------------------------- #
# 定义校验
# --------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_onboarding_definition_valid() -> None:
    """工作流定义有效：6 步、DAG 无环、依赖与 uses 正确。"""
    wf = GPUOnboardingWorkflow()
    defn = wf.build_definition()

    # 6 步
    assert len(defn.steps) == 6
    assert [s.id for s in defn.steps] == wf.ONBOARDING_STEPS

    # DAG 无环（不抛异常即通过）
    validate_dag(defn.steps)

    # 依赖与 uses 正确
    by_id = {s.id: s for s in defn.steps}
    assert by_id["discover"].needs == []
    assert by_id["discover"].uses == "gpu.discovery"
    assert by_id["validate"].needs == ["discover"]
    assert by_id["validate"].uses == "gpu.preflight"
    assert by_id["prepare"].needs == ["validate"]
    assert by_id["prepare"].uses == "model.prepare"
    assert by_id["deploy"].needs == ["prepare"]
    assert by_id["deploy"].uses == "inference.deploy"
    assert by_id["verify"].needs == ["deploy"]
    assert by_id["verify"].uses == "inference.verify"
    assert by_id["commit"].needs == ["verify"]
    assert by_id["commit"].uses == "resource.commit"


@pytest.mark.e2e
def test_onboarding_steps_order() -> None:
    """步骤顺序正确：discover → validate → prepare → deploy → verify → commit。"""
    wf = GPUOnboardingWorkflow()
    expected = ["discover", "validate", "prepare", "deploy", "verify", "commit"]

    assert expected == wf.ONBOARDING_STEPS

    defn = wf.build_definition()
    assert [s.id for s in defn.steps] == expected


# --------------------------------------------------------------------------- #
# 完整闭环
# --------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_onboarding_full_loop() -> None:
    """完整闭环执行：6 步全部成功，结果正确。"""
    wf = GPUOnboardingWorkflow()
    defn = wf.build_definition()
    inputs = _default_inputs()
    handler = MockOnboardingHandler()

    results = asyncio.run(
        _no_retry_executor().execute_workflow(defn, inputs, "key-full-loop", handler)
    )

    # 全部 6 步按序执行
    assert handler.executed == wf.ONBOARDING_STEPS
    # 每步结果正确
    assert set(results.keys()) == set(wf.ONBOARDING_STEPS)
    for step_id in wf.ONBOARDING_STEPS:
        assert results[step_id] == {"step_id": step_id, "status": "succeeded"}


# --------------------------------------------------------------------------- #
# 失败终止
# --------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_onboarding_discover_failure() -> None:
    """discover 失败时工作流终止，后续步骤不执行。"""
    wf = GPUOnboardingWorkflow()
    defn = wf.build_definition()
    inputs = _default_inputs()
    handler = MockOnboardingHandler(fail_at="discover")

    with pytest.raises(RuntimeError, match="discover"):
        asyncio.run(
            _no_retry_executor().execute_workflow(defn, inputs, "key-discover-fail", handler)
        )

    # 仅 discover 执行，后续 5 步未执行
    assert handler.executed == ["discover"]


@pytest.mark.e2e
def test_onboarding_validate_failure() -> None:
    """validate 失败时工作流终止，后续步骤不执行。"""
    wf = GPUOnboardingWorkflow()
    defn = wf.build_definition()
    inputs = _default_inputs()
    handler = MockOnboardingHandler(fail_at="validate")

    with pytest.raises(RuntimeError, match="validate"):
        asyncio.run(
            _no_retry_executor().execute_workflow(defn, inputs, "key-validate-fail", handler)
        )

    # discover + validate 执行，后续 4 步未执行
    assert handler.executed == ["discover", "validate"]


@pytest.mark.e2e
def test_onboarding_verify_failure() -> None:
    """verify 失败时工作流终止：deploy 已执行但验收不通过。"""
    wf = GPUOnboardingWorkflow()
    defn = wf.build_definition()
    inputs = _default_inputs()
    handler = MockOnboardingHandler(fail_at="verify")

    with pytest.raises(RuntimeError, match="verify"):
        asyncio.run(_no_retry_executor().execute_workflow(defn, inputs, "key-verify-fail", handler))

    # discover → validate → prepare → deploy → verify 执行，commit 未执行
    assert handler.executed == ["discover", "validate", "prepare", "deploy", "verify"]


# --------------------------------------------------------------------------- #
# WorkflowExecutor 集成
# --------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_onboarding_with_workflow_executor() -> None:
    """用 WorkflowExecutor 执行完整工作流（集成测试）。"""
    wf = GPUOnboardingWorkflow()
    defn = wf.build_definition()
    inputs = _default_inputs()
    handler = MockOnboardingHandler()

    # 使用默认执行器（含默认重试策略）
    executor = WorkflowExecutor()
    results = asyncio.run(executor.execute_workflow(defn, inputs, "key-executor", handler))

    # 全部 6 步执行
    assert handler.executed == wf.ONBOARDING_STEPS
    assert len(results) == 6

    # 工作流定义属性正确
    assert defn.name == "gpu-onboarding"
    assert defn.timeout_seconds == 1800

    # 输入被正确传递到步骤（handler 可收到 inputs）
    # 每步结果均成功
    for step_id in wf.ONBOARDING_STEPS:
        assert results[step_id]["status"] == "succeeded"


# --------------------------------------------------------------------------- #
# 持久化与恢复
# --------------------------------------------------------------------------- #


@pytest.mark.e2e
def test_onboarding_persist_and_recover() -> None:
    """持久化运行实例 + 恢复：中断后可从 WorkflowStore 恢复非终态运行。"""
    wf = GPUOnboardingWorkflow()
    defn = wf.build_definition()
    inputs = _default_inputs()

    store = WorkflowStore()

    # 1. 创建运行实例
    ref = asyncio.run(store.create_run(defn, inputs, "key-persist"))
    assert ref.run_id is not None

    # 2. 启动运行
    asyncio.run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))

    # 3. 模拟执行 discover 和 validate（步骤状态 Pending → Running → Succeeded）
    for step_id in ("discover", "validate"):
        asyncio.run(store.update_step_status(ref.run_id, step_id, StepStatus.RUNNING.value))
        asyncio.run(
            store.update_step_status(
                ref.run_id,
                step_id,
                StepStatus.SUCCEEDED.value,
                result={"step_id": step_id, "status": "succeeded"},
            )
        )

    # 4. 模拟中断：此时工作流状态仍为 Running（非终态）

    # 5. 恢复：列出非终态运行
    pending = asyncio.run(store.list_pending_runs())
    assert len(pending) == 1
    assert pending[0]["run_id"] == ref.run_id
    assert pending[0]["status"] == WorkflowStatus.RUNNING.value

    # 6. 查询已持久化的步骤状态，确认前两步已成功
    statuses = asyncio.run(store.get_step_statuses(ref.run_id))
    assert statuses["discover"] == StepStatus.SUCCEEDED
    assert statuses["validate"] == StepStatus.SUCCEEDED
    assert statuses["prepare"] == StepStatus.PENDING

    # 7. 恢复后继续执行剩余步骤
    remaining = ["prepare", "deploy", "verify", "commit"]
    for step_id in remaining:
        asyncio.run(store.update_step_status(ref.run_id, step_id, StepStatus.RUNNING.value))
        asyncio.run(
            store.update_step_status(
                ref.run_id,
                step_id,
                StepStatus.SUCCEEDED.value,
                result={"step_id": step_id, "status": "succeeded"},
            )
        )

    # 8. 标记工作流成功
    asyncio.run(store.update_run_status(ref.run_id, WorkflowStatus.SUCCEEDED.value))

    # 9. 验证最终状态
    run = asyncio.run(store.get_run(ref.run_id))
    assert run["status"] == WorkflowStatus.SUCCEEDED.value
    assert all(s["status"] == StepStatus.SUCCEEDED.value for s in run["steps"])

    # 10. 终态后不再出现在 pending 列表中
    pending_after = asyncio.run(store.list_pending_runs())
    assert len(pending_after) == 0

    asyncio.run(store.close())
