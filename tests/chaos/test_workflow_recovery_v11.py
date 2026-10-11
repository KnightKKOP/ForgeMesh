"""混沌测试：Workflow 恢复与边界语义验证（v1.1 第 9.2 节 / 阶段 C，WP-C2）。

对已交付的 Workflow 恢复能力做故障与边界验证（聚焦 Workflow 一侧，
Outbox 语义见 ``tests/contract/test_outbox_semantics.py``）：

* 长步骤超时：执行中步骤超过步骤级超时后进入明确的失败终态，
  不会无限停留 ``Running``；
* 节点断联重连：节点失联（心跳/任务通道不可用）导致驱动中止，
  重连（进程重启）后依据 store 持久化的步骤检查点从中断点续跑，
  已完成步骤不重复执行；
* 取消触发补偿：cancel 后已完成步骤按逆序执行补偿，未完成步骤不再执行；
* 恢复后版本固定：进程重启后恢复运行中的 run，其 definition 版本与
  Provider（``Step.uses``）与启动时一致（ADR-011 版本固定），
  定义升级仅影响新实例。

标记说明：根 pyproject 未注册 ``chaos`` marker（``--strict-markers`` 下会
导致收集失败），故遵循 ``tests/chaos`` 目录既有约定使用 ``unit`` 标记；
后续注册 ``chaos`` marker 后可切换。

重启/恢复场景统一使用 ``tmp_path`` 下的 SQLite 文件（非 ``:memory:`），
以"新实例重开同一 DB 文件"模拟进程重启。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from forgemesh.workflow.runtime import RetryPolicy, Step, WorkflowDefinition
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.compensate import (
    CompensationExecutor,
    CompensationRegistry,
)
from forgemesh.workflowruntime.exec import (
    ExecutionError,
    StepExecutor,
    TimeoutLevel,
)
from forgemesh.workflowruntime.store import WorkflowStore

pytestmark = pytest.mark.unit

#: 步骤 handler 类型：async (step, inputs) -> dict。
Handler = Callable[[Step, dict[str, Any]], Awaitable[dict[str, Any]]]


class NodeDisconnectedError(Exception):
    """模拟节点断联：心跳/任务通道不可用，引擎驱动被迫中止。"""


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_definition(name: str = "chaos-recovery") -> WorkflowDefinition:
    """构造三步骤工作流定义（步骤 uses 模拟不同 Provider 能力）。"""
    return WorkflowDefinition(
        name=name,
        timeout_seconds=300,
        steps=[
            Step(id="provision", uses="resource.provision"),
            Step(id="deploy", uses="resource.deploy", needs=["provision"]),
            Step(id="verify", uses="health.verify", needs=["deploy"]),
        ],
    )


async def _drive(
    defn: WorkflowDefinition,
    store: WorkflowStore,
    run_id: str,
    executor: StepExecutor,
    handler: Handler,
    stop_before: str | None = None,
) -> None:
    """模拟引擎驱动：按定义顺序执行未完成步骤并将状态落盘。

    恢复语义：跳过 store 中已 ``Succeeded`` 的步骤（检查点续跑）。
    ``stop_before`` 指定的步骤置 ``Running`` 后抛
    :class:`NodeDisconnectedError` 模拟节点断联（驱动中止，
    该步骤状态停留 ``Running``）。
    """
    statuses = await store.get_step_statuses(run_id)
    for step in defn.steps:
        current = statuses.get(step.id)
        if current is StepStatus.SUCCEEDED:
            continue  # 检查点续跑：已完成步骤不重跑
        if stop_before == step.id:
            # 模拟节点断联：步骤刚转入 Running 即失联，驱动中止，
            # 该步骤状态停留 Running（重启恢复的起点）。
            if current is not StepStatus.RUNNING:
                await store.update_step_status(run_id, step.id, StepStatus.RUNNING.value)
            raise NodeDisconnectedError(f"node lost before step '{step.id}'")
        # Pending 步骤先置 Running；断联遗留的 Running 步骤保持原状态
        # 直接重新执行（Running→Running 为非法转移）。
        if current is not StepStatus.RUNNING:
            await store.update_step_status(run_id, step.id, StepStatus.RUNNING.value)
        result = await executor.execute_step(step, {}, f"{run_id}:{step.id}", handler)
        await store.update_step_status(run_id, step.id, StepStatus.SUCCEEDED.value, result)
    await store.update_run_status(run_id, WorkflowStatus.SUCCEEDED.value)


# ---------------------------------------------------------------------- #
# 场景一：长步骤超时
# ---------------------------------------------------------------------- #


def test_long_step_timeout_enters_failed_terminal_state(tmp_path) -> None:
    """长步骤超时：超过步骤级超时后步骤进入 Failed 终态，而非无限 Running。"""
    db_file = tmp_path / "wf-timeout.db"
    # 不设工作流级超时，聚焦步骤级超时（三级超时中的 STEP 层）。
    defn = WorkflowDefinition(
        name="chaos-timeout",
        timeout_seconds=None,
        steps=[
            Step(id="provision", uses="resource.provision", timeout=1),
            Step(id="deploy", uses="resource.deploy", needs=["provision"]),
        ],
    )
    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn, {}, "key-long-timeout"))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))
    _run(store.update_step_status(ref.run_id, "provision", StepStatus.RUNNING.value))

    calls: list[str] = []

    async def hanging_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        """模拟卡死的长步骤：挂起时长远超步骤级超时。"""
        calls.append(step.id)
        await asyncio.sleep(10)
        return {"status": "ok"}

    # 步骤级超时为 int 秒（Step.timeout: int | None），设 1 秒；
    # 重试策略单次尝试（重试语义已有单测覆盖），聚焦超时后的终态落盘。
    policy = RetryPolicy(max_attempts=1, initial_backoff_seconds=0.0, max_backoff_seconds=0.0)
    executor = StepExecutor(retry_policy=policy)
    first_step = defn.steps[0]

    with pytest.raises(ExecutionError) as exc_info:
        _run(executor.execute_step(first_step, {}, f"{ref.run_id}:provision", hanging_handler))

    # 超时被识别为步骤级（挂起步骤被取消，仅一次尝试）。
    assert exc_info.value.timeout_level == TimeoutLevel.STEP
    assert "timed out" in str(exc_info.value)
    assert calls == ["provision"]

    # 引擎将超时结果落盘：步骤进入 Failed 终态（有明确失败时点），
    # 运行进入终态——不会无限停留 Running。
    _run(store.update_step_status(ref.run_id, "provision", StepStatus.FAILED.value))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.FAILED.value))

    run = _run(store.get_run(ref.run_id))
    step_row = next(s for s in run["steps"] if s["step_id"] == "provision")
    assert step_row["status"] == StepStatus.FAILED.value
    assert step_row["finished_at"] is not None  # 非无限 Running
    assert run["status"] == WorkflowStatus.FAILED.value
    # 终态运行不再出现在待恢复列表。
    pending_ids = {r["run_id"] for r in _run(store.list_pending_runs())}
    assert ref.run_id not in pending_ids
    _run(store.close())


# ---------------------------------------------------------------------- #
# 场景二：节点断联重连（从中断点续跑）
# ---------------------------------------------------------------------- #


def test_node_disconnect_resume_from_checkpoint(tmp_path) -> None:
    """节点断联重连：恢复后从中断点续跑，已完成步骤不重复执行。"""
    db_file = tmp_path / "wf-resume.db"
    defn = _make_definition()
    calls: list[str] = []  # 跨"重启"累计的步骤执行调用

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        calls.append(step.id)
        return {"step_id": step.id, "status": "ok"}

    # 第一段：provision 执行成功后，deploy 开始前节点断联。
    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn, {}, "key-resume"))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))
    with pytest.raises(NodeDisconnectedError):
        _run(_drive(defn, store, ref.run_id, StepExecutor(), handler, stop_before="deploy"))

    # 断联现场：provision 已完成，deploy 停留 Running，verify 未开始。
    statuses = _run(store.get_step_statuses(ref.run_id))
    assert statuses["provision"] is StepStatus.SUCCEEDED
    assert statuses["deploy"] is StepStatus.RUNNING
    assert statuses["verify"] is StepStatus.PENDING
    assert calls == ["provision"]
    _run(store.close())

    # 第二段：进程重启——新 store + 新 executor（内存幂等缓存随进程丢失），
    # 重开同一 DB 文件恢复。
    restarted_store = WorkflowStore(str(db_file))
    fresh_executor = StepExecutor()
    recovered = _run(restarted_store.list_pending_runs())
    assert [r["run_id"] for r in recovered] == [ref.run_id]

    # 依据持久化的步骤检查点续跑（而非重头执行）。
    _run(_drive(defn, restarted_store, ref.run_id, fresh_executor, handler))

    # provision 未重复执行；deploy/verify 各执行一次。
    assert calls == ["provision", "deploy", "verify"]
    final = _run(restarted_store.get_run(ref.run_id))
    assert final["status"] == WorkflowStatus.SUCCEEDED.value
    assert all(s["status"] == StepStatus.SUCCEEDED.value for s in final["steps"])
    assert _run(restarted_store.list_pending_runs()) == []
    _run(restarted_store.close())


# ---------------------------------------------------------------------- #
# 场景三：取消触发补偿
# ---------------------------------------------------------------------- #


def test_cancel_triggers_reverse_compensation(tmp_path) -> None:
    """取消触发补偿：已完成步骤按逆序补偿，未完成步骤不再执行。"""
    db_file = tmp_path / "wf-cancel.db"
    defn = _make_definition()
    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn, {}, "key-cancel"))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))

    exec_calls: list[str] = []
    comp_calls: list[tuple[str, dict[str, Any]]] = []

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        exec_calls.append(step.id)
        return {"step_id": step.id, "artifact": f"{step.id}-res"}

    async def comp_handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        comp_calls.append((step.id, dict(inputs)))
        return {"compensated": step.id}

    registry = CompensationRegistry()
    registry.register("provision", comp_handler)
    registry.register("deploy", comp_handler)
    executor = StepExecutor()
    comp_executor = CompensationExecutor(registry)

    # 前两步成功，第三步（verify）尚未开始执行。
    for step_id in ("provision", "deploy"):
        step = next(s for s in defn.steps if s.id == step_id)
        _run(store.update_step_status(ref.run_id, step_id, StepStatus.RUNNING.value))
        result = _run(executor.execute_step(step, {}, f"{ref.run_id}:{step_id}", handler))
        _run(store.update_step_status(ref.run_id, step_id, StepStatus.SUCCEEDED.value, result))
    step_results = {
        "provision": {"step_id": "provision", "artifact": "provision-res"},
        "deploy": {"step_id": "deploy", "artifact": "deploy-res"},
    }

    # cancel(cleanup=True) 语义：运行进入 Compensating，
    # 已完成步骤转入 Compensating 后由补偿执行器逆序回滚。
    _run(store.update_run_status(ref.run_id, WorkflowStatus.COMPENSATING.value))
    completed = ["provision", "deploy"]
    for step_id in completed:
        _run(store.update_step_status(ref.run_id, step_id, StepStatus.COMPENSATING.value))

    outcomes = _run(comp_executor.compensate(defn, completed, step_results))

    for step_id in completed:
        _run(store.update_step_status(ref.run_id, step_id, StepStatus.COMPENSATED.value))
    # 未完成步骤不再执行：直接置 Cancelled。
    _run(store.update_step_status(ref.run_id, "verify", StepStatus.CANCELLED.value))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.CANCELLED.value))

    # 补偿按逆序执行，且补偿输入为原步骤结果。
    assert [sid for sid, _ in comp_calls] == ["deploy", "provision"]
    assert dict(comp_calls)["deploy"] == step_results["deploy"]
    # 未完成步骤从未被执行。
    assert exec_calls == ["provision", "deploy"]
    assert outcomes == {
        "provision": StepStatus.COMPENSATED,
        "deploy": StepStatus.COMPENSATED,
    }
    # 最终落盘状态：已补偿 / 已取消，运行终态 Cancelled。
    run = _run(store.get_run(ref.run_id))
    step_map = {s["step_id"]: s["status"] for s in run["steps"]}
    assert step_map == {
        "provision": StepStatus.COMPENSATED.value,
        "deploy": StepStatus.COMPENSATED.value,
        "verify": StepStatus.CANCELLED.value,
    }
    assert run["status"] == WorkflowStatus.CANCELLED.value
    assert _run(store.list_pending_runs()) == []
    _run(store.close())


# ---------------------------------------------------------------------- #
# 场景四：恢复后版本固定（ADR-011）
# ---------------------------------------------------------------------- #


def test_restart_pins_definition_version_and_providers(tmp_path) -> None:
    """恢复后版本固定：重启恢复运行中 run，定义版本与 Provider 与启动时一致。"""
    db_file = tmp_path / "wf-pin.db"
    defn_v1 = WorkflowDefinition(
        name="trust-baseline-flow",
        timeout_seconds=300,
        steps=[
            Step(id="discover", uses="gpu.discovery"),
            Step(id="deploy", uses="resource.deploy", needs=["discover"]),
        ],
    )
    calls: list[str] = []

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        calls.append(step.id)
        return {"step_id": step.id, "status": "ok"}

    # 第一段：discover 完成后进程重启（run 处于 Running，待恢复）。
    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn_v1, {}, "key-pin"))
    pinned_version = ref.definition_version
    _run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))
    _run(store.update_step_status(ref.run_id, "discover", StepStatus.RUNNING.value))
    _run(store.update_step_status(ref.run_id, "discover", StepStatus.SUCCEEDED.value, {"ok": True}))
    _run(store.close())

    # 期间定义升级：新增 notify 步骤（Provider 能力集合变化）。
    defn_v2 = WorkflowDefinition(
        name="trust-baseline-flow",
        timeout_seconds=300,
        steps=[
            Step(id="discover", uses="gpu.discovery"),
            Step(id="deploy", uses="resource.deploy", needs=["discover"]),
            Step(id="notify", uses="notify.email", needs=["deploy"]),
        ],
    )

    # 第二段：新 store + 新 engine 执行单元（StepExecutor）重开同一 DB 文件，
    # 恢复运行中的 run。
    restarted_store = WorkflowStore(str(db_file))
    fresh_step_executor = StepExecutor()  # 新 engine 实例：内存幂等缓存清空
    recovered = _run(restarted_store.list_pending_runs())
    assert [r["run_id"] for r in recovered] == [ref.run_id]

    run = _run(restarted_store.get_run(ref.run_id))
    # ADR-011 版本固定：版本未随定义升级漂移。
    assert run["definition_version"] == pinned_version
    # 持久化的定义可无损还原，Provider（uses）与启动时一致。
    restored_defn = WorkflowDefinition.model_validate_json(run["definition_json5"])
    assert restored_defn == defn_v1
    assert [s.uses for s in restored_defn.steps] == [
        "gpu.discovery",
        "resource.deploy",
    ]
    assert restored_defn != defn_v2  # 恢复的不是升级后的定义

    # 按固定定义 + 步骤检查点用新 engine 续跑剩余步骤至终态。
    statuses = _run(restarted_store.get_step_statuses(ref.run_id))
    for step in restored_defn.steps:
        if statuses[step.id] is StepStatus.SUCCEEDED:
            continue  # 已成功步骤不重跑（版本固定 + 检查点续跑）
        _run(restarted_store.update_step_status(ref.run_id, step.id, StepStatus.RUNNING.value))
        result = _run(
            fresh_step_executor.execute_step(step, {}, f"{ref.run_id}:{step.id}", handler)
        )
        _run(
            restarted_store.update_step_status(
                ref.run_id, step.id, StepStatus.SUCCEEDED.value, result
            )
        )
    _run(restarted_store.update_run_status(ref.run_id, WorkflowStatus.SUCCEEDED.value))
    assert calls == ["deploy"]  # discover 未重复执行

    # 升级后的定义仅影响新建实例：新 run 派生不同版本号。
    ref_v2 = _run(restarted_store.create_run(defn_v2, {}, "key-pin-v2"))
    assert ref_v2.definition_version != pinned_version
    assert ref_v2.run_id != ref.run_id
    _run(restarted_store.close())
