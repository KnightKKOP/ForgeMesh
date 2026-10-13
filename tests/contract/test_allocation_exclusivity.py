"""Allocation 唯一 Active 契约测试（任务 61 / WP-C1，v1.1 §9.2/§9.3 阶段 C）。

验证 ``allocations`` 表部分唯一索引
``idx_allocations_one_active_per_tenant_resource``（``WHERE status = 'Active'``，
``registry/store.py`` DDL）兜底的「同一租户内同一 ``resource_id``（归属资源 uid）
至多一个 Active 分配」：

* 并发创建同一 resource_id 的两个 Active Allocation：恰好一个成功，另一个
  因唯一索引失败（实现包装为 :class:`ConflictError`）；
* Active 释放（``Active → Released`` 状态机转移）后可重新分配；
* 不同 resource_id 的 Active 分配互不影响。

并发模型说明
------------
同 ``test_registry_consistency.py``：采用两个独立 ``SQLiteRegistry`` 实例指向
同一 DB 文件（多副本部署形态）。唯一索引由 SQLite 在文件写锁串行化下强制执行，
无论两个 INSERT 如何交错，第二个到达者必然命中索引冲突。

缺陷 R-1（已修复，同 test_registry_consistency.py）
----------------------------------------------------
修复前单实例共享连接无锁，并发 ``put_allocation`` 交错时失败方 ``rollback()``
（store.py ``IntegrityError`` 处理分支）会连带回滚成功方未提交的 INSERT，出现
「两个调用一胜一负，但胜者记录丢失 → Active 计数为 0」的静默丢失。现在共享连接由
实例级锁串行化；本文件并发用例采用独立连接形态验证唯一索引契约本身。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import ConflictError
from forgemesh.resource.allocation import Allocation

TENANT = "tenant-1"


def _run(coro):
    """同步驱动协程（项目测试不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_allocation(
    resource_id: str,
    idempotency_key: str,
    tenant_id: str = TENANT,
) -> Allocation:
    """构造最小 Allocation（status 默认 Active）。"""
    return Allocation(
        resource_id=resource_id,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    )


def _count_active(reg: SQLiteRegistry, resource_id: str) -> int:
    """统计某 resource_id 当前 Active 记录数（直读 DB 的证据口径）。"""
    row = reg._conn.execute(
        "SELECT COUNT(*) AS n FROM allocations "
        "WHERE resource_id=? AND status='Active'",
        (resource_id,),
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------- #
# 并发创建同一 resource_id 的两个 Active：恰好一个成功
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_concurrent_put_allocation_single_active_winner(tmp_path) -> None:
    """并发双写同一 resource_id：唯一索引兜底，恰好一胜一负且 Active 恰为 1。"""
    db_file = tmp_path / "registry.db"

    async def scenario() -> None:
        writer1 = SQLiteRegistry(str(db_file))
        writer2 = SQLiteRegistry(str(db_file))
        try:
            # 两写入方并发为同一资源创建 Active 分配（不同幂等键）
            results = await asyncio.gather(
                writer1.put_allocation(
                    _make_allocation("gpu-0", idempotency_key="idem-a")
                ),
                writer2.put_allocation(
                    _make_allocation("gpu-0", idempotency_key="idem-b")
                ),
                return_exceptions=True,
            )

            winners = [r for r in results if not isinstance(r, BaseException)]
            losers = [r for r in results if isinstance(r, BaseException)]
            # 恰好一个成功（返回新分配 id）
            assert len(winners) == 1
            winner_id = winners[0]
            assert isinstance(winner_id, str) and winner_id
            # 恰好一个失败：唯一索引冲突 → 实现包装的 ConflictError
            assert len(losers) == 1
            assert isinstance(losers[0], ConflictError)

            # 胜者的分配真实落盘：Active 记录存在且 id 一致、恰为一条
            active = await writer1.get_active_allocation("gpu-0", TENANT)
            assert active is not None
            assert active["id"] == winner_id
            assert active["status"] == "Active"
            assert _count_active(writer1, "gpu-0") == 1
        finally:
            await writer1.close()
            await writer2.close()

    asyncio.run(scenario())


@pytest.mark.contract
def test_sequential_second_active_rejected_by_unique_index() -> None:
    """顺序路径：同一 resource_id 的第二个 Active 同样被唯一索引拒绝。"""
    reg = SQLiteRegistry()
    try:
        _run(reg.put_allocation(_make_allocation("gpu-0", idempotency_key="idem-1")))

        with pytest.raises(ConflictError, match="active allocation"):
            _run(reg.put_allocation(_make_allocation("gpu-0", idempotency_key="idem-2")))

        # 拒绝后状态不受污染：Active 仍为首个分配
        active = _run(reg.get_active_allocation("gpu-0", TENANT))
        assert active is not None
        assert _count_active(reg, "gpu-0") == 1
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# Active 释放后可重新分配
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_release_then_reallocate_same_resource() -> None:
    """Active → Released 后，同一 resource_id 可再次分配（新 Active 成立）。"""
    reg = SQLiteRegistry()
    try:
        first_id = _run(
            reg.put_allocation(_make_allocation("gpu-0", idempotency_key="idem-1"))
        )

        # 释放后不再持有 Active
        _run(reg.release_allocation(first_id, TENANT, reason="contract-test"))
        assert _run(reg.get_active_allocation("gpu-0", TENANT)) is None
        assert _count_active(reg, "gpu-0") == 0

        # 同一 resource_id 可重新分配：部分唯一索引仅约束 Active 存量
        second_id = _run(
            reg.put_allocation(_make_allocation("gpu-0", idempotency_key="idem-2"))
        )
        assert second_id != first_id

        active = _run(reg.get_active_allocation("gpu-0", TENANT))
        assert active is not None
        assert active["id"] == second_id
        assert active["status"] == "Active"

        # 历史保留：旧分配处于 Released 终态，Active 记录恰为一条
        rows = _run(reg.list_allocations(TENANT))
        assert sorted(r["status"] for r in rows) == ["Active", "Released"]
        assert _count_active(reg, "gpu-0") == 1
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# 不同 resource_id 互不影响
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_distinct_resource_ids_independent(tmp_path) -> None:
    """并发为不同 resource_id 创建 Active：互不影响，全部成功。"""
    db_file = tmp_path / "registry.db"

    async def scenario() -> None:
        writer1 = SQLiteRegistry(str(db_file))
        writer2 = SQLiteRegistry(str(db_file))
        try:
            ids = await asyncio.gather(
                writer1.put_allocation(
                    _make_allocation("gpu-0", idempotency_key="idem-gpu0")
                ),
                writer2.put_allocation(
                    _make_allocation("gpu-1", idempotency_key="idem-gpu1")
                ),
            )
            assert all(isinstance(i, str) and i for i in ids)

            # 两个资源的 Active 分配各自成立，唯一索引不跨 resource_id 约束
            for resource_id in ("gpu-0", "gpu-1"):
                active = await writer1.get_active_allocation(resource_id, TENANT)
                assert active is not None
                assert active["status"] == "Active"
                assert _count_active(writer1, resource_id) == 1
        finally:
            await writer1.close()
            await writer2.close()

    asyncio.run(scenario())
