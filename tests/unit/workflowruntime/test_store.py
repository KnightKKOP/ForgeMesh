"""WorkflowStore SQLite 持久化单元测试（对齐 development-plan 5.4 / ADR-011）。

覆盖：
* 幂等创建（同一 idempotency_key 返回同一 run_id）；
* 不同 key 返回不同 run_id；
* 查询运行实例；
* 运行状态合法/非法转移；
* 步骤状态更新；
* 列出非终态运行；
* 获取所有步骤状态；
* 版本固定。
"""

from __future__ import annotations

import pytest
from forgemesh.core.errors import InvalidStateTransition, NotFoundError
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.store import WorkflowStore


def _make_definition(
    name: str = "deploy-flow",
    timeout_seconds: int | None = 300,
) -> WorkflowDefinition:
    """构造测试用工作流定义（含两个步骤）。"""
    return WorkflowDefinition(
        name=name,
        timeout_seconds=timeout_seconds,
        steps=[
            Step(id="prepare", uses="resource.prepare"),
            Step(id="deploy", uses="resource.deploy", needs=["prepare"]),
        ],
    )


# --------------------------------------------------------------------------- #
# create_run 幂等
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_create_run_idempotent() -> None:
    """同一 idempotency_key 重复调用返回同一 run_id。"""
    store = WorkflowStore()
    defn = _make_definition()

    ref1 = asyncio_run(store.create_run(defn, {"model": "a"}, "key-1"))
    ref2 = asyncio_run(store.create_run(defn, {"model": "a"}, "key-1"))

    assert ref1.run_id == ref2.run_id
    assert ref1.definition_version == ref2.definition_version


@pytest.mark.unit
def test_create_run_different_keys() -> None:
    """不同 idempotency_key 返回不同 run_id。"""
    store = WorkflowStore()
    defn = _make_definition()

    ref1 = asyncio_run(store.create_run(defn, {}, "key-a"))
    ref2 = asyncio_run(store.create_run(defn, {}, "key-b"))

    assert ref1.run_id != ref2.run_id


# --------------------------------------------------------------------------- #
# get_run
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_get_run() -> None:
    """创建后可查询运行实例（含步骤状态）。"""
    store = WorkflowStore()
    defn = _make_definition()

    ref = asyncio_run(store.create_run(defn, {"model": "test"}, "key-get"))

    run = asyncio_run(store.get_run(ref.run_id))
    assert run["run_id"] == ref.run_id
    assert run["definition_name"] == "deploy-flow"
    assert run["status"] == WorkflowStatus.PENDING.value
    assert run["inputs"] == {"model": "test"}
    assert run["timeout_seconds"] == 300
    # 步骤已初始化为 Pending。
    assert len(run["steps"]) == 2
    assert all(s["status"] == StepStatus.PENDING.value for s in run["steps"])


@pytest.mark.unit
def test_get_run_not_found() -> None:
    """查询不存在的 run_id 抛 NotFoundError。"""
    store = WorkflowStore()
    with pytest.raises(NotFoundError):
        asyncio_run(store.get_run("nonexistent-run"))


# --------------------------------------------------------------------------- #
# update_run_status
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_update_run_status_valid() -> None:
    """合法转移 Pending→Running 成功。"""
    store = WorkflowStore()
    defn = _make_definition()
    ref = asyncio_run(store.create_run(defn, {}, "key-valid"))

    asyncio_run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))

    run = asyncio_run(store.get_run(ref.run_id))
    assert run["status"] == WorkflowStatus.RUNNING.value


@pytest.mark.unit
def test_update_run_status_invalid() -> None:
    """非法转移 Pending→Succeeded 抛 InvalidStateTransition。"""
    store = WorkflowStore()
    defn = _make_definition()
    ref = asyncio_run(store.create_run(defn, {}, "key-invalid"))

    with pytest.raises(InvalidStateTransition):
        asyncio_run(store.update_run_status(ref.run_id, WorkflowStatus.SUCCEEDED.value))


@pytest.mark.unit
def test_update_run_status_not_found() -> None:
    """更新不存在的 run_id 抛 NotFoundError。"""
    store = WorkflowStore()
    with pytest.raises(NotFoundError):
        asyncio_run(store.update_run_status("no-such-run", WorkflowStatus.RUNNING.value))


# --------------------------------------------------------------------------- #
# update_step_status
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_update_step_status() -> None:
    """步骤状态更新（Pending→Running→Succeeded）。"""
    store = WorkflowStore()
    defn = _make_definition()
    ref = asyncio_run(store.create_run(defn, {}, "key-step"))

    # Pending → Running
    asyncio_run(
        store.update_step_status(ref.run_id, "prepare", StepStatus.RUNNING.value)
    )
    statuses = asyncio_run(store.get_step_statuses(ref.run_id))
    assert statuses["prepare"] == StepStatus.RUNNING

    # Running → Succeeded（带结果）
    asyncio_run(
        store.update_step_status(
            ref.run_id, "prepare", StepStatus.SUCCEEDED.value, result={"output": "ok"}
        )
    )
    run = asyncio_run(store.get_run(ref.run_id))
    prepare_step = next(s for s in run["steps"] if s["step_id"] == "prepare")
    assert prepare_step["status"] == StepStatus.SUCCEEDED.value
    assert prepare_step["result"] == {"output": "ok"}
    assert prepare_step["finished_at"] is not None


@pytest.mark.unit
def test_update_step_status_invalid_transition() -> None:
    """步骤非法转移（Pending→Succeeded）抛 InvalidStateTransition。"""
    store = WorkflowStore()
    defn = _make_definition()
    ref = asyncio_run(store.create_run(defn, {}, "key-step-invalid"))

    with pytest.raises(InvalidStateTransition):
        asyncio_run(
            store.update_step_status(ref.run_id, "prepare", StepStatus.SUCCEEDED.value)
        )


# --------------------------------------------------------------------------- #
# list_pending_runs
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_list_pending_runs() -> None:
    """列出非终态运行（终态运行被排除）。"""
    store = WorkflowStore()
    defn = _make_definition()

    ref1 = asyncio_run(store.create_run(defn, {}, "pending-1"))
    ref2 = asyncio_run(store.create_run(defn, {}, "pending-2"))
    ref3 = asyncio_run(store.create_run(defn, {}, "done-1"))

    # ref1: Pending → Running（非终态）
    asyncio_run(store.update_run_status(ref1.run_id, WorkflowStatus.RUNNING.value))
    # ref3: Pending → Running → Succeeded（终态）
    asyncio_run(store.update_run_status(ref3.run_id, WorkflowStatus.RUNNING.value))
    asyncio_run(store.update_run_status(ref3.run_id, WorkflowStatus.SUCCEEDED.value))

    pending = asyncio_run(store.list_pending_runs())
    pending_ids = {r["run_id"] for r in pending}

    assert ref1.run_id in pending_ids
    assert ref2.run_id in pending_ids
    assert ref3.run_id not in pending_ids  # 终态被排除


# --------------------------------------------------------------------------- #
# get_step_statuses
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_get_step_statuses() -> None:
    """获取所有步骤状态。"""
    store = WorkflowStore()
    defn = _make_definition()
    ref = asyncio_run(store.create_run(defn, {}, "key-statuses"))

    statuses = asyncio_run(store.get_step_statuses(ref.run_id))
    assert set(statuses.keys()) == {"prepare", "deploy"}
    assert all(s == StepStatus.PENDING for s in statuses.values())


@pytest.mark.unit
def test_get_step_statuses_not_found() -> None:
    """获取不存在运行的步骤状态抛 NotFoundError。"""
    store = WorkflowStore()
    with pytest.raises(NotFoundError):
        asyncio_run(store.get_step_statuses("no-such-run"))


# --------------------------------------------------------------------------- #
# 版本固定
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_version_fixed() -> None:
    """definition_version 在创建时固定，同一定义内容派生同一版本。"""
    store = WorkflowStore()
    defn = _make_definition()

    ref = asyncio_run(store.create_run(defn, {}, "key-version"))
    run = asyncio_run(store.get_run(ref.run_id))

    # RunRef 与 DB 中记录的版本一致。
    assert ref.definition_version == run["definition_version"]
    assert ref.definition_version > 0

    # 同一定义内容再次创建（不同 key）应派生同一版本号。
    ref2 = asyncio_run(store.create_run(defn, {}, "key-version-2"))
    assert ref2.definition_version == ref.definition_version


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def asyncio_run(coro):
    """用 asyncio.run 驱动 async 方法。"""
    import asyncio

    return asyncio.run(coro)
