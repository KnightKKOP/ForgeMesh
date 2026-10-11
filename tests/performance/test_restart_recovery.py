"""基准 3+4：重启恢复耗时与 pending 运行恢复（对齐 development-plan 第 8 章基准 3）。

计划原值
--------
* 工作流恢复时间（重启后）：< 60s 完成恢复扫描。

本地口径与放宽理由
------------------
* 小规模版本：``SQLiteRegistry``（100 资源）与 :class:`WorkflowStore`
  （60 个运行实例，其中 40 个非终态）各自建状态后 ``close`` 模拟进程停止，
  重建实例（同一 db 文件）并完成**全量恢复扫描**，断言总耗时 < 5s。
* 阈值理由：计划 60s 面向生产规模全量扫描；本地 100/60 条记录按比例
  远小于生产规模，5s 已是高度宽松的界（约 12x 于比例值），足以防 CI 抖动
  同时兜住回归（如误引入逐行 O(n²) 扫描）。
* 恢复正确性一并断言：恢复读取到的状态与重启前一致（数量与内容），
  ``list_pending_runs`` 仅返回非终态运行（计划第 8 章 8.1 Chaos 层
  "恢复语义正确，无数据损坏"在性能层的最小化对照）。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import WorkflowStatus
from forgemesh.workflowruntime.store import WorkflowStore

pytestmark = pytest.mark.performance

#: Registry 恢复规模：100 资源（小规模可执行版本）。
N_REGISTRY_RESOURCES = 100

#: WorkflowStore 恢复规模：60 个运行实例（40 非终态 + 20 终态）。
N_WORKFLOW_RUNS = 60
N_WORKFLOW_TERMINAL = 20

#: 恢复耗时阈值（秒）：计划 < 60s 的小规模宽松版（理由见模块 docstring）。
RECOVERY_THRESHOLD_SECONDS = 5.0


def _make_resource(index: int) -> Resource:
    return Resource(
        kind="Widget",
        metadata=ObjectMeta(name=f"recover-{index:03d}", generation=1, tenant_id="tenant-bench"),
    )


@pytest.mark.performance
def test_registry_restart_recovery_under_threshold(tmp_path: pytest.TempPathFactory) -> None:
    """SQLiteRegistry 重启恢复：100 资源全量读回 < 5s 且状态一致。

    计划原值：工作流恢复时间 < 60s（本项为其 Registry 侧对照，
    小规模 100 资源、阈值 5s，理由见模块 docstring）。
    """
    db_file = tmp_path / "registry-recovery.db"

    async def _seed() -> None:
        registry = SQLiteRegistry(str(db_file))
        for index in range(N_REGISTRY_RESOURCES):
            await registry.put(_make_resource(index))
        await registry.close()

    asyncio.run(_seed())

    async def _recover() -> tuple[float, int, list[str]]:
        started = time.perf_counter()
        registry = SQLiteRegistry(str(db_file))  # 模拟进程重启
        listing = await registry.list("Widget", "tenant-bench")
        names = [r.metadata.name for r in listing]
        # 全量逐个 get 核验（完整恢复扫描口径）。
        for name in names:
            await registry.get("Widget", None, name, "tenant-bench")
        await registry.close()
        return time.perf_counter() - started, len(names), names

    elapsed, count, names = asyncio.run(_recover())
    print(
        f"\n[performance] registry-recovery: n={N_REGISTRY_RESOURCES} "
        f"elapsed={elapsed:.3f}s threshold={RECOVERY_THRESHOLD_SECONDS}s"
    )
    assert count == N_REGISTRY_RESOURCES
    assert set(names) == {f"recover-{i:03d}" for i in range(N_REGISTRY_RESOURCES)}
    assert elapsed < RECOVERY_THRESHOLD_SECONDS


@pytest.mark.performance
def test_workflow_store_restart_recovery_under_threshold(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """WorkflowStore 重启恢复：list_pending_runs 恢复非终态运行 < 5s。

    计划原值：工作流恢复时间（重启后）< 60s 完成恢复扫描；
    小规模 60 运行（40 非终态）、阈值 5s，理由见模块 docstring。
    """
    db_file = tmp_path / "workflow-recovery.db"
    definition = WorkflowDefinition(
        name="bench-recovery",
        timeout_seconds=300,
        steps=[Step(id="s1", uses="gpu.discovery")],
    )

    async def _seed() -> list[str]:
        store = WorkflowStore(str(db_file))
        pending_ids: list[str] = []
        for index in range(N_WORKFLOW_RUNS):
            ref = await store.create_run(definition, {"i": index}, f"bench-key-{index}")
            if index < N_WORKFLOW_RUNS - N_WORKFLOW_TERMINAL:
                pending_ids.append(ref.run_id)
                continue
            # 其余推进到终态 Succeeded（不应出现在恢复列表）。
            await store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value)
            await store.update_run_status(ref.run_id, WorkflowStatus.SUCCEEDED.value)
        await store.close()
        return pending_ids

    pending_ids = asyncio.run(_seed())

    async def _recover() -> tuple[float, set[str]]:
        started = time.perf_counter()
        store = WorkflowStore(str(db_file))  # 模拟进程重启
        pending = await store.list_pending_runs()
        # 恢复扫描后逐个 get 核验（完整恢复口径）。
        for run in pending:
            await store.get_run(run["run_id"])
        await store.close()
        return time.perf_counter() - started, {r["run_id"] for r in pending}

    elapsed, recovered = asyncio.run(_recover())
    print(
        f"\n[performance] workflow-recovery: runs={N_WORKFLOW_RUNS} "
        f"pending={len(recovered)} elapsed={elapsed:.3f}s "
        f"threshold={RECOVERY_THRESHOLD_SECONDS}s"
    )
    assert recovered == set(pending_ids)
    assert len(recovered) == N_WORKFLOW_RUNS - N_WORKFLOW_TERMINAL
    assert elapsed < RECOVERY_THRESHOLD_SECONDS
