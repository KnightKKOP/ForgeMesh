"""Registry 一致性契约测试（任务 61 / WP-C1，v1.1 §9.2-9.3 阶段 C）。

覆盖 ``SQLiteRegistry`` 的核心一致性契约：

* CAS 并发冲突：两个写入方带相同 ``expected_version`` 并发 put，恰好一个
  成功、一个抛 :class:`ConflictError`，且成功方写入真实落盘；
* 租户隔离不可绕过：跨租户 get / list / delete 均不可见（NotFound 语义）；
* ``resource_version`` 随成功 put 严格单调 +1；
* 同一 DB 文件重启恢复：资源、版本、finalizers 完好；
* 备份恢复：复制 SQLite 文件后以新 store 打开副本，数据完整一致。

并发模型说明（对齐实现的真实并发形态）
----------------------------------------
``SQLiteRegistry`` 为异步 API（``asyncio.to_thread`` 包装同步 sqlite3 调用，
见 ``registry/store.py``）。并发测试采用**两个独立 ``SQLiteRegistry`` 实例
指向同一 DB 文件**（即控制平面多副本 / 多连接部署形态），由 SQLite 文件写锁
串行化写事务，``UPDATE ... WHERE resource_version=?`` 提供原子 CAS。该形态
经 100 轮并发验证全部满足「恰好一胜 + 终态正确」（见下方缺陷记录的对照）。

缺陷 R-1（已修复）
------------------
``SQLiteRegistry`` 的所有异步方法共享同一个 ``sqlite3.Connection``。修复前该连接
没有任何锁保护，**同一实例**上的两个异步方法经 ``asyncio.to_thread`` 在两个线程
并发写（单进程多任务并发场景）时，连接级事务交错会导致：

1. **成功方写入丢失**：失败方 ``self._conn.rollback()``（store.py CAS 路径
   ``rowcount == 0`` 分支 / ``IntegrityError`` 分支）回滚连接级共享事务，
   连带丢弃成功方尚未 commit 的 UPDATE；成功方随后 ``commit()`` 因无活动
   事务成为 no-op（观测样本：``OperationalError: cannot commit - no
   transaction is active``），**静默返回成功对象但数据未持久化**；
2. **双失败**：两个并发 CAS 均抛 ``ConflictError``，写入整体丢失；
3. **底层错误泄漏**：``sqlite3.InterfaceError: bad parameter or other API
   misuse`` 直接抛给调用方。

修复前 200 轮并发探测中约 31 轮（≈15%）出现上述任一现象。现在每次数据库调用都在
实例级 ``threading.RLock`` 内执行（``registry/store.py`` ``_run``），同一实例并发写的
回归用例见 ``test_registry_hardening.py``。本文件的并发测试采用「独立实例 × 同一
DB 文件」形态，验证 CAS 契约本身。
"""

from __future__ import annotations

import asyncio
import shutil

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource

KIND = "Node"  # 不受管 kind：无 phase，绕开状态机干扰，聚焦 Registry 语义
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _make_resource(
    name: str = "demo",
    namespace: str = "ns-a",
    tenant_id: str = TENANT_A,
    kind: str = KIND,
    uid: str = "uid-1",
    generation: int = 0,
) -> Resource:
    """构造最小 Resource（复用既有单测的构造方式）。"""
    return Resource(
        kind=kind,
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            uid=uid,
            tenant_id=tenant_id,
            generation=generation,
        ),
    )


def _run(coro):
    """同步驱动协程（项目测试不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# CAS 并发冲突：两个写入方 + 相同 expected_version
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_cas_concurrent_update_exactly_one_wins(tmp_path) -> None:
    """两个写入方以相同 expected_version 并发 CAS，恰好一胜一负且落盘。

    采用两个独立 store 实例指向同一 DB 文件（多副本部署形态）：SQLite
    文件写锁串行化写事务，CAS 由 ``UPDATE ... WHERE resource_version=?``
    的原子性保证——无论线程如何交错，第二个到达的 UPDATE 匹配 0 行转为
    ``ConflictError``。
    """
    db_file = tmp_path / "registry.db"

    async def scenario() -> None:
        writer1 = SQLiteRegistry(str(db_file))
        writer2 = SQLiteRegistry(str(db_file))
        try:
            created = await writer1.put(_make_resource(name="demo"))
            assert created.metadata.resourceVersion == "0"

            # 两写入方并发以 expected_version="0" 提交（真实线程并发）
            results = await asyncio.gather(
                writer1.put(created, expected_version="0"),
                writer2.put(created, expected_version="0"),
                return_exceptions=True,
            )

            winners = [r for r in results if not isinstance(r, BaseException)]
            losers = [r for r in results if isinstance(r, BaseException)]
            # 恰好一个成功
            assert len(winners) == 1
            assert winners[0].metadata.resourceVersion == "1"
            # 恰好一个失败，且为 ConflictError（而非其他泄漏的底层异常）
            assert len(losers) == 1
            assert isinstance(losers[0], ConflictError)

            # 成功方的写入真实落盘：终态版本为 1，两连接视角一致
            v1 = await writer1.get(KIND, "ns-a", "demo", TENANT_A)
            v2 = await writer2.get(KIND, "ns-a", "demo", TENANT_A)
            assert v1.metadata.resourceVersion == "1"
            assert v2.metadata.resourceVersion == "1"
        finally:
            await writer1.close()
            await writer2.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# 租户隔离不可绕过
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_tenant_isolation_cannot_be_bypassed(tmp_path) -> None:
    """A 租户资源对 B 租户完全不可见：get/list/delete 均以 NotFound 拒绝。"""
    reg = SQLiteRegistry(str(tmp_path / "registry.db"))
    try:
        _run(reg.put(_make_resource(name="secret", uid="uid-a")))

        # 跨租户 get：报 NotFound，而非返回数据（不泄露存在性）
        with pytest.raises(NotFoundError):
            _run(reg.get(KIND, "ns-a", "secret", TENANT_B))

        # 跨租户 list：结果为空
        assert _run(reg.list(KIND, TENANT_B)) == []
        assert _run(reg.list(KIND, TENANT_B, namespace="ns-a")) == []

        # 跨租户 delete 同样不可绕过（无该租户视角下的目标记录）
        with pytest.raises(NotFoundError):
            _run(reg.delete(KIND, "ns-a", "secret", TENANT_B))

        # 上述跨租户操作不产生任何副作用：A 租户视角资源完好
        own = _run(reg.get(KIND, "ns-a", "secret", TENANT_A))
        assert own.metadata.name == "secret"
        assert [r.metadata.name for r in _run(reg.list(KIND, TENANT_A))] == ["secret"]
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# 资源版本单调递增
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_resource_version_strictly_monotonic() -> None:
    """每次成功 put 后 resource_version 严格 +1（Create 为 0，CAS 逐次递增）。"""
    reg = SQLiteRegistry()
    try:
        current = _run(reg.put(_make_resource()))
        assert current.metadata.resourceVersion == "0"

        # 连续 4 次成功 CAS：版本严格单调 +1，无跳变、无回退
        for expected in range(4):
            current = _run(reg.put(current, expected_version=str(expected)))
            assert current.metadata.resourceVersion == str(expected + 1)

        # 冲突的 CAS 不推进版本（失败方不得影响单调性）
        stale = _run(reg.get(KIND, "ns-a", "demo", TENANT_A))
        with pytest.raises(ConflictError):
            _run(reg.put(stale, expected_version="0"))
        final = _run(reg.get(KIND, "ns-a", "demo", TENANT_A))
        assert final.metadata.resourceVersion == "4"
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# DB 文件重启恢复
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_restart_recovery_same_db_file(tmp_path) -> None:
    """同一 SQLite 文件重新打开 store 后，资源 / 版本 / finalizers 完好。"""
    db_file = tmp_path / "registry.db"

    first = SQLiteRegistry(str(db_file))
    try:
        # 写入 3 个资源，其中 demo 做两次 CAS 演进（终态版本 2）并挂 finalizer
        created = _run(first.put(_make_resource(name="demo")))
        v1 = _run(first.put(created, expected_version="0"))
        v1.finalizers = ["example.com/cleanup"]
        v2 = _run(first.put(v1, expected_version="1"))
        assert v2.metadata.resourceVersion == "2"
        _run(first.put(_make_resource(name="other-1", uid="uid-2")))
        _run(first.put(_make_resource(name="other-2", uid="uid-3")))
    finally:
        _run(first.close())

    # 模拟进程重启：全新 store 实例、同一 DB 文件
    restarted = SQLiteRegistry(str(db_file))
    try:
        demo = _run(restarted.get(KIND, "ns-a", "demo", TENANT_A))
        assert demo.metadata.name == "demo"
        assert demo.metadata.resourceVersion == "2"
        assert demo.metadata.uid == "uid-1"
        assert demo.finalizers == ["example.com/cleanup"]

        names = sorted(r.metadata.name for r in _run(restarted.list(KIND, TENANT_A)))
        assert names == ["demo", "other-1", "other-2"]

        # 重启后可基于恢复的版本继续 CAS 演进（版本链未断裂）
        evolved = _run(restarted.put(demo, expected_version="2"))
        assert evolved.metadata.resourceVersion == "3"
    finally:
        _run(restarted.close())


# ---------------------------------------------------------------------- #
# 备份恢复
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_backup_restore_from_copied_db_file(tmp_path) -> None:
    """复制 SQLite 文件 → 新 store 打开副本 → 数据完整一致且可独立演进。"""
    db_file = tmp_path / "registry.db"
    backup_file = tmp_path / "registry-backup.db"

    source = SQLiteRegistry(str(db_file))
    try:
        created = _run(source.put(_make_resource(name="svc", uid="uid-svc")))
        created.metadata.labels = {"env": "prod"}
        _run(source.put(created, expected_version="0"))
        _run(source.put(_make_resource(name="node", uid="uid-node", generation=7)))
    finally:
        # 先关闭源连接再复制：最后一个连接关闭时 WAL 合并回主库文件，数据全量落盘
        _run(source.close())

    # 模拟备份：复制 DB 文件（文件级备份恢复路径）

    shutil.copyfile(db_file, backup_file)

    restored = SQLiteRegistry(str(backup_file))
    try:
        # 逐资源对比：名称 / uid / 版本 / generation / labels 一致
        svc = _run(restored.get(KIND, "ns-a", "svc", TENANT_A))
        assert svc.metadata.uid == "uid-svc"
        assert svc.metadata.resourceVersion == "1"
        assert svc.metadata.labels == {"env": "prod"}
        node = _run(restored.get(KIND, "ns-a", "node", TENANT_A))
        assert node.metadata.uid == "uid-node"
        assert node.metadata.generation == 7
        assert node.metadata.resourceVersion == "0"

        names = sorted(r.metadata.name for r in _run(restored.list(KIND, TENANT_A)))
        assert names == ["node", "svc"]

        # 副本可独立继续演进（恢复出的版本链可直接 CAS）
        evolved = _run(restored.put(svc, expected_version="1"))
        assert evolved.metadata.resourceVersion == "2"
    finally:
        _run(restored.close())
