"""基准 4：Registry 并发冲突正确率（对齐 development-plan 第 8 章基准 4）。

计划原值
--------
* 并发分配冲突正确率：100%（无双重独占）。

本地口径与阈值策略
------------------
* 以 Registry 乐观并发控制（Update-CAS）为口径：``asyncio.gather`` 并发
  ``put(expected_version="0")`` 同一资源，断言**成功恰一次**、其余全部被
  :class:`~forgemesh.core.errors.ConflictError` 正确拒绝（100% 正确率），
  且落库最终版本恰为 ``"1"``（无双重写入）。
* 正确率是**正确性断言**，不做倍数放宽（100% 即 100%）。
* 实现方式（实验证据）：同一 ``SQLiteRegistry`` 实例的底层 sqlite3 连接
  被 ``asyncio.to_thread`` 多线程并发使用时，Python 3.12 sqlite3 会抛
  ``InterfaceError``（同连接跨线程事务交错）；因此每个并发任务打开指向
  **同一数据库文件**的独立实例，模拟多实例/多进程对共享权威状态的并发
  写入（更贴近生产语义，30 轮 × 16 并发实验 0 异常）。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import ConflictError
from forgemesh.core.objects import ObjectMeta, Resource

pytestmark = pytest.mark.performance

#: 每轮并发数：足够的并发度暴露 CAS 竞态窗口。
N_CONCURRENT = 16

#: 轮数：多轮增强置信（每轮独立资源）。
N_ROUNDS = 3


def _make_resource(name: str) -> Resource:
    return Resource(
        kind="Widget",
        metadata=ObjectMeta(name=name, generation=2, tenant_id="tenant-bench"),
    )


def _make_cas_candidate(name: str) -> Resource:
    """构造携带期望版本 ``"0"`` 的 CAS 更新候选（generation 2）。"""
    resource = _make_resource(name)
    resource.metadata.resourceVersion = "0"
    return resource


@pytest.mark.performance
def test_concurrent_cas_exactly_one_winner(tmp_path: pytest.TempPathFactory) -> None:
    """并发 CAS put 同一资源：成功恰一次、冲突 100% 被拒、无双重写入。

    计划原值：并发分配冲突正确率 100%（无双重独占）；
    正确性指标不放宽阈值。
    """
    db_file = tmp_path / "conflict.db"

    async def _cas_once(name: str) -> str:
        """独立 registry 实例对资源 ``name`` 执行一次 CAS put，返回结果分类。"""
        registry = SQLiteRegistry(str(db_file))
        try:
            candidate = _make_cas_candidate(name)
            await registry.put(candidate, expected_version="0")
            return "ok"
        except ConflictError:
            return "conflict"
        finally:
            await registry.close()

    async def _round(round_index: int) -> tuple[int, int, str]:
        name = f"contended-{round_index}"
        # 建立 v0 基线（Create）。
        seeder = SQLiteRegistry(str(db_file))
        try:
            await seeder.put(_make_resource(name))
        finally:
            await seeder.close()
        # 并发 CAS：每任务独立实例（理由见模块 docstring）。
        results = await asyncio.gather(*(_cas_once(name) for _ in range(N_CONCURRENT)))
        # 落库终态核验：无双重写入 → 版本恰为 "1"。
        verifier = SQLiteRegistry(str(db_file))
        try:
            final = await verifier.get("Widget", None, name, "tenant-bench")
        finally:
            await verifier.close()
        return results.count("ok"), results.count("conflict"), final.metadata.resourceVersion

    total_ok = total_conflict = 0
    for round_index in range(N_ROUNDS):
        ok, conflict, final_version = asyncio.run(_round(round_index))
        total_ok += ok
        total_conflict += conflict
        assert ok == 1, f"round {round_index}: 并发 CAS 成功 {ok} 次（期望恰 1 次）——出现双重写入"
        assert conflict == N_CONCURRENT - 1, (
            f"round {round_index}: 冲突被正确拒绝 {conflict} 次"
            f"（期望 {N_CONCURRENT - 1} 次，100% 正确率）"
        )
        assert final_version == "1", f"round {round_index}: 落库版本 {final_version!r}（期望 '1'）"

    # 正确率口径：16 个并发写中每轮仅 1 个合法成功，其余 (N-1) 个应被
    # 拒绝；分母为"应被拒绝的冲突预期总数"，而非全部并发数。
    expected_conflicts = N_ROUNDS * (N_CONCURRENT - 1)
    correctness = total_conflict / expected_conflicts
    assert total_ok == N_ROUNDS, f"并发 CAS 总成功 {total_ok} 次（期望每轮恰 1 次）"
    print(
        f"\n[performance] conflict-correctness: rounds={N_ROUNDS} "
        f"concurrency={N_CONCURRENT} ok={total_ok} conflict={total_conflict} "
        f"correctness={correctness:.2%}"
    )
    assert correctness == 1.0
