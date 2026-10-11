"""ProcessedKeyStore 调和去重持久化契约测试（任务 61 / WP-C1，v1.1 §9.3）。

验证 ``ProcessedKeyStore``（``reconciler/dedup.py``，对齐 architecture §5.5
「不能依赖单进程内存状态保证全局正确性」）的核心契约：

* 同一 key 重复 ``mark`` 幂等（不抛错、不产生重复行）；
* 模拟进程重启（新 store 实例打开同一 DB 文件）后 key 仍判已处理；
* 未处理 key 判定 not processed；
* 事务边界：``mark`` 写入失败（如磁盘错误）时异常传播且**不产生假
  「已处理」**——失败路径不留下半提交状态。

事务边界说明（按实现能力断言）
------------------------------
``ProcessedKeyStore`` 使用独立 SQLite 连接与独立事务，**不提供**与应用
副作用（Registry 写入等）共享同一事务的 API；「标记与副作用」的原子性由
协调器协议保证：``ReconciliationController`` 在副作用失败路径**不**登记
``dedup_key``（``reconciler/core.py`` Terminating 子流程，失败直接 return，
不调用 ``_mark_processed``），requeue 后整体重试。因此本文件验证的边界是
「标记写入自身失败 → 无假已处理」，跨库同事务能力以记录形式留档（见模块
docstring 末尾）。

并发防护对比：``ProcessedKeyStore`` 临界区以 ``threading.Lock`` 串行化
（``dedup.py`` ``__init__``），规避了 ``SQLiteRegistry`` 的缺陷 R-1（单实例
共享连接无锁并发交错，见 ``test_registry_consistency.py`` 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from forgemesh.controlplane.reconciler import ProcessedKeyStore


def _run(coro):
    """同步驱动协程（项目测试不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# 未处理 key 判定
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_unseen_key_reports_not_processed() -> None:
    """未标记的 key 判定 not processed；不同 key 互不影响。"""
    store = ProcessedKeyStore()
    try:
        assert _run(store.seen("key-never-marked")) is False
        assert _run(store.seen("another-key")) is False

        _run(store.mark("key-a"))
        # 已标记 key 为 True，其余 key 仍为 False（精确匹配，非前缀/子串）
        assert _run(store.seen("key-a")) is True
        assert _run(store.seen("key-ab")) is False
        assert _run(store.seen("key")) is False
    finally:
        _run(store.close())


# ---------------------------------------------------------------------- #
# 同一 key 二次标记幂等
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_mark_twice_idempotent(tmp_path) -> None:
    """同一 key 重复 mark 幂等：不抛错、seen 仍 True、物理上仅一行。"""
    db_file = tmp_path / "dedup.db"
    store = ProcessedKeyStore(str(db_file))
    try:
        _run(store.mark("key-a"))
        _run(store.mark("key-a"))  # 二次标记：不抛错
        _run(store.mark("key-a"))  # 三次标记：仍不抛错

        assert _run(store.seen("key-a")) is True

        # INSERT OR REPLACE 语义：物理上仅一行，无重复记录堆积
        conn = sqlite3.connect(str(db_file))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM processed_dedup_keys WHERE dedup_key='key-a'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1
    finally:
        _run(store.close())


# ---------------------------------------------------------------------- #
# 模拟进程重启：新 store 实例 + 同一 DB 文件
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_restart_recovery_marks_persist(tmp_path) -> None:
    """进程重启（新实例、同一 DB 文件）后 key 仍判已处理（§5.5 核心诉求）。"""
    db_file = tmp_path / "dedup.db"

    first = ProcessedKeyStore(str(db_file))
    try:
        _run(first.mark("key-a"))
        _run(first.mark("key-b"))
        assert _run(first.seen("key-a")) is True
    finally:
        _run(first.close())

    # 模拟进程重启：全新实例、同一数据库文件（内存 set 方案在此会失效）
    restarted = ProcessedKeyStore(str(db_file))
    try:
        assert _run(restarted.seen("key-a")) is True
        assert _run(restarted.seen("key-b")) is True
        assert _run(restarted.seen("key-c")) is False  # 未标记的仍为 False
    finally:
        _run(restarted.close())


# ---------------------------------------------------------------------- #
# 事务边界：标记写入失败不产生假「已处理」
# ---------------------------------------------------------------------- #


class _FailingInsertConn:
    """连接代理：对 INSERT OR REPLACE 语句模拟写入失败，其余调用转发。

    仅用于测试 ``mark`` 的失败路径：注入 ``sqlite3.OperationalError``
    模拟磁盘满 / IO 错误等底层写入失败，验证异常传播且无半提交状态。
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, params: tuple = ()):  # noqa: ANN001
        if "INSERT OR REPLACE" in sql:
            raise sqlite3.OperationalError("simulated mark write failure")
        return self._real.execute(sql, params)

    def __getattr__(self, name: str):  # noqa: ANN001
        # commit / rollback / row_factory 等其余成员全部转发真实连接
        return getattr(self._real, name)


@pytest.mark.contract
def test_mark_write_failure_does_not_create_false_positive(tmp_path) -> None:
    """mark 写入失败：异常传播，且失败后 key 不被判为已处理（无假阳性）。"""
    db_file = tmp_path / "dedup.db"
    store = ProcessedKeyStore(str(db_file))
    try:
        real_conn = store._conn
        # 注入写入失败：后续 INSERT OR REPLACE（mark 的落库语句）抛错
        store._conn = _FailingInsertConn(real_conn)
        with pytest.raises(sqlite3.OperationalError):
            _run(store.mark("key-a"))

        # 还原真实连接：失败路径未留下任何「已处理」状态
        store._conn = real_conn
        assert _run(store.seen("key-a")) is False

        # 物理层同样无残留行（失败事务未产生半提交数据）
        conn = sqlite3.connect(str(db_file))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM processed_dedup_keys WHERE dedup_key='key-a'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

        # 恢复后重新 mark 可成功：失败不永久破坏存储
        _run(store.mark("key-a"))
        assert _run(store.seen("key-a")) is True
    finally:
        _run(store.close())
