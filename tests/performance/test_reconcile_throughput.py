"""基准 1+2：调和吞吐与单轮 P99 延迟（对齐 development-plan 第 8 章基准 1/2）。

计划原值
--------
* API P99 延迟（网关→控制平面）：< 200ms
* 协调吞吐：≥ 50 resource/s（单实例）

本地口径与放宽理由
------------------
* Python 侧无 Go 网关，以「Registry put（建立期望状态）+ 9 步 reconcile」
  的单资源端到端耗时近似 API P99 口径；P99 阈值取计划值的 5 倍（1000ms），
  吸收 CI 机器抖动与 asyncio.to_thread 线程池调度开销。
* 吞吐阈值取计划下限 50 res/s 的 1/5（≥ 10 res/s）防 CI 抖动；本地实测
  通常远高于此，实测值随断言输出供校准参考（M4 前校准计划值）。
* 使用内存 SQLite（``:memory:``）排除磁盘 IO 抖动，聚焦调和路径本身。
* 计时用 :func:`time.perf_counter`；P99 为手写 nearest-rank 分位数，
  不引入新依赖。

注意：50 个资源各自使用独立 dedup_key（generation 递增派生），避免被
幂等去重短路，确保测得真实调和路径。
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest
from forgemesh.controlplane.reconciler import ReconciliationController, compute_dedup_key
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.reconciler import ReconcileContext
from forgemesh.policy.api import PolicyDecision

pytestmark = pytest.mark.performance

#: 资源数量：对齐计划"协调吞吐 ≥ 50 resource/s"的批量口径。
N_RESOURCES = 50

#: P99 阈值（秒）：计划 API P99 < 200ms 的 5 倍放宽（防 CI 抖动）。
P99_THRESHOLD_SECONDS = 1.0

#: 吞吐下限（资源/秒）：计划 ≥ 50 res/s 的 1/5 放宽（防 CI 抖动）。
THROUGHPUT_FLOOR = 10.0


def _percentile(samples: list[float], pct: float) -> float:
    """nearest-rank 分位数（手写，不引入新依赖）。

    Args:
        samples: 样本列表（秒）。
        pct: 分位百分比（如 99.0 表示 P99）。

    Returns:
        对应分位的样本值。
    """
    ordered = sorted(samples)
    idx = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[idx]


class _AllowPolicy:
    """固定 allow 的 PolicyEngine 测试替身（聚焦调和路径性能）。"""

    async def check(self, ctx, action):  # noqa: ANN001
        return PolicyDecision(decision="allow", reason="performance-benchmark")


class _BenchmarkScheduler:
    """无外部副作用的 Scheduler 测试替身（排除真实 GPU 分配开销）。"""

    async def allocate(self, req):  # noqa: ANN001
        from forgemesh.core.scheduler import AllocationRef

        return AllocationRef(allocation_id="alloc-bench", resource_id="node-bench")


class _BenchmarkWorkflowRuntime:
    """无外部副作用的 WorkflowRuntime 测试替身（排除真实工作流执行开销）。"""

    async def start(self, defn, inputs, idempotency_key):  # noqa: ANN001
        from forgemesh.workflow.runtime import RunRef

        return RunRef(run_id=idempotency_key, definition_version=1)


def _make_resource(index: int) -> Resource:
    """构造第 ``index`` 个测试资源（独立 name/generation → 独立 dedup_key）。"""
    return Resource(
        kind="Widget",
        metadata=ObjectMeta(name=f"bench-{index:03d}", generation=1, tenant_id="tenant-bench"),
    )


@pytest.mark.performance
def test_reconcile_throughput_and_p99() -> None:
    """批量 put+reconcile 50 资源：吞吐 ≥ 10 res/s 且单轮 P99 < 1000ms。

    计划原值：API P99 < 200ms、协调吞吐 ≥ 50 resource/s（单实例）；
    阈值取 5x 放宽防 CI 抖动（理由见模块 docstring）。
    """

    async def _scenario() -> tuple[float, float, float]:
        registry = SQLiteRegistry(":memory:")
        controller = ReconciliationController(
            registry=registry,
            policy_engine=_AllowPolicy(),
            scheduler=_BenchmarkScheduler(),
            workflow_runtime=_BenchmarkWorkflowRuntime(),
        )
        latencies: list[float] = []
        loop_start = time.perf_counter()
        for index in range(N_RESOURCES):
            resource = _make_resource(index)
            dedup = compute_dedup_key(
                "Widget", None, resource.metadata.name, None, 1, "reconcile"
            )
            ctx = ReconcileContext(
                resource=resource,
                observed={},
                correlation_id=f"bench-{index}",
                tenant_id="tenant-bench",
                dedup_key=dedup.key,
            )
            started = time.perf_counter()
            await registry.put(resource)  # 建立期望状态（Create，v0）
            result = await controller.reconcile(ctx)  # 9 步协调（差异收敛路径）
            latencies.append(time.perf_counter() - started)
            assert result.done is True
        total = time.perf_counter() - loop_start
        await registry.close()

        throughput = N_RESOURCES / total
        return throughput, _percentile(latencies, 99.0), total

    throughput, p99, total = _run(_scenario())
    print(
        f"\n[performance] reconcile: n={N_RESOURCES} total={total:.3f}s "
        f"throughput={throughput:.1f} res/s p99={p99 * 1000:.1f}ms"
    )
    assert throughput >= THROUGHPUT_FLOOR, (
        f"协调吞吐 {throughput:.1f} res/s 低于阈值 {THROUGHPUT_FLOOR} res/s"
        f"（计划值 50 res/s，1/5 放宽）"
    )
    assert p99 < P99_THRESHOLD_SECONDS, (
        f"单轮调和 P99 {p99 * 1000:.1f}ms 超过阈值 {P99_THRESHOLD_SECONDS * 1000:.0f}ms"
        f"（计划 API P99 200ms，5x 放宽）"
    )


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)
