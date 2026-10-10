"""混沌测试：控制平面重启（INV-4）。

验证控制平面重启后工作流可恢复：

* ``WorkflowStore`` 持久化到文件（非 ``:memory:``），重启后可恢复；
* ``list_pending_runs()`` 返回非终态运行（Pending / Running / Waiting）；
* 终态运行（Succeeded）不在恢复列表中；
* 运行状态与步骤状态可正确恢复；
* 恢复后可继续推进运行至终态。

使用 ``tmp_path`` 提供文件 SQLite 路径，模拟进程重启。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.store import WorkflowStore

pytestmark = pytest.mark.unit


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_definition(name: str = "chaos-restart") -> WorkflowDefinition:
    """构造含单步骤的工作流定义。"""
    return WorkflowDefinition(
        name=name,
        timeout_seconds=300,
        steps=[Step(id="s1", uses="gpu.discovery")],
    )


# ---------------------------------------------------------------------- #
# 重启后 list_pending_runs 恢复非终态运行
# ---------------------------------------------------------------------- #


def test_restart_recovers_non_terminal_runs(tmp_path: pytest.TempPathFactory) -> None:
    """重启后 list_pending_runs 返回 Pending/Running/Waiting，不含终态。"""
    db_file = tmp_path / "cp.db"
    defn = _make_definition()

    # 第一次启动：创建 4 个运行，分别推进到不同状态
    store = WorkflowStore(str(db_file))
    ref_pending = _run(store.create_run(defn, {}, "key-pending"))
    ref_running = _run(store.create_run(defn, {}, "key-running"))
    ref_waiting = _run(store.create_run(defn, {}, "key-waiting"))
    ref_succeeded = _run(store.create_run(defn, {}, "key-succeeded"))

    _run(store.update_run_status(ref_running.run_id, WorkflowStatus.RUNNING.value))
    _run(store.update_run_status(ref_waiting.run_id, WorkflowStatus.RUNNING.value))
    _run(store.update_run_status(ref_waiting.run_id, WorkflowStatus.WAITING.value))
    _run(store.update_run_status(ref_succeeded.run_id, WorkflowStatus.RUNNING.value))
    _run(store.update_run_status(ref_succeeded.run_id, WorkflowStatus.SUCCEEDED.value))
    _run(store.close())

    # 模拟控制平面重启：重新打开同一数据库文件
    restarted = WorkflowStore(str(db_file))
    pending = _run(restarted.list_pending_runs())
    run_ids = {r["run_id"] for r in pending}

    assert ref_pending.run_id in run_ids
    assert ref_running.run_id in run_ids
    assert ref_waiting.run_id in run_ids
    assert ref_succeeded.run_id not in run_ids
    assert len(pending) == 3
    _run(restarted.close())


def test_restart_preserves_run_status(tmp_path: pytest.TempPathFactory) -> None:
    """重启后运行状态可正确恢复。"""
    db_file = tmp_path / "cp.db"
    defn = _make_definition()

    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn, {}, "key-1"))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.WAITING.value))
    _run(store.close())

    restarted = WorkflowStore(str(db_file))
    run = _run(restarted.get_run(ref.run_id))
    assert run["status"] == WorkflowStatus.WAITING.value
    _run(restarted.close())


def test_restart_preserves_step_status(tmp_path: pytest.TempPathFactory) -> None:
    """重启后步骤状态可正确恢复。"""
    db_file = tmp_path / "cp.db"
    defn = _make_definition()

    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn, {}, "key-1"))
    _run(store.update_step_status(ref.run_id, "s1", StepStatus.RUNNING.value))
    _run(
        store.update_step_status(
            ref.run_id, "s1", StepStatus.SUCCEEDED.value, {"ok": True}
        )
    )
    _run(store.close())

    restarted = WorkflowStore(str(db_file))
    statuses = _run(restarted.get_step_statuses(ref.run_id))
    assert statuses["s1"] is StepStatus.SUCCEEDED
    _run(restarted.close())


def test_restart_preserves_definition_version(tmp_path: pytest.TempPathFactory) -> None:
    """重启后定义版本固定不变（ADR-011 版本固定语义）。"""
    db_file = tmp_path / "cp.db"
    defn = _make_definition()

    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn, {}, "key-1"))
    _run(store.close())

    restarted = WorkflowStore(str(db_file))
    run = _run(restarted.get_run(ref.run_id))
    assert run["definition_version"] == ref.definition_version
    _run(restarted.close())


# ---------------------------------------------------------------------- #
# 恢复后可继续推进运行
# ---------------------------------------------------------------------- #


def test_restart_then_continue_run(tmp_path: pytest.TempPathFactory) -> None:
    """重启后可继续推进恢复的运行至终态，再次重启不再出现。"""
    db_file = tmp_path / "cp.db"
    defn = _make_definition()

    store = WorkflowStore(str(db_file))
    ref = _run(store.create_run(defn, {}, "key-1"))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value))
    _run(store.update_run_status(ref.run_id, WorkflowStatus.WAITING.value))
    _run(store.close())

    # 重启后恢复并继续推进至终态
    restarted = WorkflowStore(str(db_file))
    pending = _run(restarted.list_pending_runs())
    assert len(pending) == 1
    run_id = pending[0]["run_id"]
    _run(restarted.update_run_status(run_id, WorkflowStatus.RUNNING.value))
    _run(restarted.update_run_status(run_id, WorkflowStatus.SUCCEEDED.value))
    _run(restarted.close())

    # 再次重启：已终态，不再出现在待恢复列表
    final = WorkflowStore(str(db_file))
    assert _run(final.list_pending_runs()) == []
    _run(final.close())
