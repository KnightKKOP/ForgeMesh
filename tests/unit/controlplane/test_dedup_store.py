"""ProcessedKeyStore（调和去重键 SQLite 持久化）单元测试。

覆盖（对齐 architecture §5.5「不能依赖单进程内存状态保证全局正确性」）：

* ``seen`` / ``mark`` 往返与幂等；
* TTL 惰性清理：过期键不再被 ``seen`` 报告，并在后续查询/写入时按间隔删除；
* 重启恢复：新 store 实例打开同一 db 文件后仍 ``seen``；
* ``ReconciliationController`` 注入 ``processed_store`` 后的幂等语义
  （含"控制器重启后仍不重复副作用"），以及默认 ``None`` 时的内存回退
  （向后兼容）。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from forgemesh.controlplane.deployment import LAUNCH_INSTANCE, LaunchedInstance
from forgemesh.controlplane.reconciler import ProcessedKeyStore, ReconciliationController
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.reconciler import ReconcileContext
from forgemesh.policy.api import PolicyDecision
from forgemesh.resource.inference import (
    AcceleratorSpec,
    EndpointProtocol,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)
from forgemesh.workflow.runtime import RunInfo, RunRef
from forgemesh.workflow.states import StepStatus, WorkflowStatus


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# seen / mark 往返与幂等
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_seen_mark_roundtrip_memory() -> None:
    """未标记的键 seen=False；mark 后 seen=True；不同键互不影响。"""
    store = ProcessedKeyStore()
    assert _run(store.seen("key-a")) is False
    _run(store.mark("key-a"))
    assert _run(store.seen("key-a")) is True
    assert _run(store.seen("key-b")) is False
    _run(store.close())


@pytest.mark.unit
def test_mark_is_idempotent() -> None:
    """重复 mark 同一键幂等：不报错、seen 仍为 True。"""
    store = ProcessedKeyStore()
    _run(store.mark("key-a"))
    _run(store.mark("key-a"))
    _run(store.mark("key-a"))
    assert _run(store.seen("key-a")) is True
    _run(store.close())


@pytest.mark.unit
def test_concurrent_seen_and_mark() -> None:
    """并发 seen/mark（asyncio.gather）不崩溃且结果一致。

    Python 3.12 sqlite3 对同一连接的跨线程事务交错会抛 ``InterfaceError``；
    ProcessedKeyStore 以内部锁串行化临界区，此处验证该防护。
    """

    async def _scenario() -> None:
        store = ProcessedKeyStore()
        await asyncio.gather(*(store.mark(f"key-{i}") for i in range(32)))
        results = await asyncio.gather(*(store.seen(f"key-{i}") for i in range(32)))
        assert all(results) is True
        absent = await asyncio.gather(*(store.seen(f"other-{i}") for i in range(8)))
        assert any(absent) is False
        await store.close()

    _run(_scenario())


# ---------------------------------------------------------------------- #
# TTL 惰性清理
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_ttl_expiry_purges_key() -> None:
    """TTL 过期后 seen=False（惰性清理在查询时触发）。"""
    store = ProcessedKeyStore(ttl_seconds=0.05)
    _run(store.mark("key-a"))
    assert _run(store.seen("key-a")) is True
    time.sleep(0.1)
    assert _run(store.seen("key-a")) is False
    _run(store.close())


@pytest.mark.unit
def test_ttl_default_keeps_fresh_keys() -> None:
    """默认 TTL（24h）下刚标记的键不会被误删。"""
    store = ProcessedKeyStore()
    _run(store.mark("key-a"))
    assert _run(store.seen("key-a")) is True
    _run(store.close())


@pytest.mark.unit
def test_expired_keys_are_physically_deleted(tmp_path: pytest.TempPathFactory) -> None:
    """过期键被物理删除（行数归零），而非仅查询过滤。"""
    import sqlite3

    db_file = tmp_path / "dedup.db"
    store = ProcessedKeyStore(str(db_file), ttl_seconds=0.05)
    _run(store.mark("key-a"))
    time.sleep(0.1)
    assert _run(store.seen("key-a")) is False

    conn = sqlite3.connect(str(db_file))
    try:
        count = conn.execute("SELECT COUNT(*) FROM processed_dedup_keys").fetchone()[0]
    finally:
        conn.close()
    assert count == 0
    _run(store.close())


@pytest.mark.unit
def test_expired_key_is_not_seen_before_it_is_deleted(tmp_path: pytest.TempPathFactory) -> None:
    """过期键在被物理删除之前 seen 也返回 False（结果不取决于删除时机）。"""
    import sqlite3

    db_file = tmp_path / "dedup.db"
    store = ProcessedKeyStore(str(db_file), ttl_seconds=0.05)
    store._purge_interval = 3600.0
    _run(store.mark("key-a"))
    time.sleep(0.1)
    assert _run(store.seen("key-a")) is False

    conn = sqlite3.connect(str(db_file))
    try:
        count = conn.execute("SELECT COUNT(*) FROM processed_dedup_keys").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    _run(store.close())


@pytest.mark.unit
def test_purges_at_most_once_per_interval() -> None:
    """seen / mark 不是每次都执行 DELETE：一个清理间隔内最多删除一次过期键。"""
    store = ProcessedKeyStore()
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    for i in range(5):
        _run(store.mark(f"key-{i}"))
        assert _run(store.seen(f"key-{i}")) is True
    deletes = [s for s in statements if s.lstrip().upper().startswith("DELETE")]
    assert len(deletes) == 1
    _run(store.close())


# ---------------------------------------------------------------------- #
# 重启恢复
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_restart_recovery_same_db_file(tmp_path: pytest.TempPathFactory) -> None:
    """新 store 实例打开同一 db 文件后仍 seen（进程重启不失效）。"""
    db_file = tmp_path / "dedup.db"

    first = ProcessedKeyStore(str(db_file))
    _run(first.mark("key-a"))
    _run(first.mark("key-b"))
    _run(first.close())

    # 模拟进程重启：全新实例、同一数据库文件。
    restarted = ProcessedKeyStore(str(db_file))
    assert _run(restarted.seen("key-a")) is True
    assert _run(restarted.seen("key-b")) is True
    assert _run(restarted.seen("key-c")) is False
    _run(restarted.close())


# ---------------------------------------------------------------------- #
# ReconciliationController 注入
# ---------------------------------------------------------------------- #


class _AllowPolicy:
    """固定 allow 的 PolicyEngine 测试替身。"""

    async def check(self, ctx, action):  # noqa: ANN001
        return PolicyDecision(decision="allow", reason="benchmark")


class _SucceededWorkflowRuntime:
    """部署运行总是已成功的 WorkflowRuntime 测试替身，记录查询次数（协调的副作用）。"""

    def __init__(self) -> None:
        self.lookups = 0

    async def start(self, defn, inputs, idempotency_key):  # noqa: ANN001
        return RunRef(run_id=idempotency_key, definition_version=1)

    async def find_run(self, idempotency_key: str) -> RunInfo:
        self.lookups += 1
        instance = LaunchedInstance(
            instance_id="instance-1",
            url="http://10.0.0.5:8000",
            protocol=EndpointProtocol.OPENAI_CHAT,
        )
        return RunInfo(
            run_id=idempotency_key,
            status=WorkflowStatus.SUCCEEDED,
            steps={LAUNCH_INSTANCE: StepStatus.SUCCEEDED},
            outputs={LAUNCH_INSTANCE: instance.model_dump(mode="json")},
        )


class _NoopRelease:
    async def release(self, resource: Resource) -> None:
        return None


def _controller(
    workflow: _SucceededWorkflowRuntime, store: ProcessedKeyStore | None = None
) -> ReconciliationController:
    return ReconciliationController(
        registry=None,
        policy_engine=_AllowPolicy(),
        workflow_runtime=workflow,
        release_provider=_NoopRelease(),
        processed_store=store,
    )


def _make_ctx(dedup_key: str) -> ReconcileContext:
    """部署最后一轮（Verifying、运行已成功）的上下文：这一轮进入 Ready 并登记去重键。"""
    resource = InferenceService(
        metadata=ObjectMeta(name="svc-1", uid="uid-1", generation=1, tenant_id="tenant-1"),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=InferencePhase.VERIFYING),
    )
    return ReconcileContext(
        resource=resource,
        observed={"phase": "Verifying"},
        correlation_id="corr-1",
        tenant_id="tenant-1",
        dedup_key=dedup_key,
    )


@pytest.mark.unit
def test_controller_with_processed_store_is_idempotent_across_restart(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """注入持久化 store：控制器重启（新实例）后同一 dedup_key 不再产生副作用。

    这是 architecture §5.5 的核心诉求：内存 set 在进程重启后失效，会导致
    重复调度副作用；持久化 store 使幂等检查跨重启生效。
    """
    db_file = tmp_path / "dedup.db"
    workflow = _SucceededWorkflowRuntime()

    async def _scenario() -> None:
        store = ProcessedKeyStore(str(db_file))
        controller = _controller(workflow, store)
        result = await controller.reconcile(_make_ctx("key-1"))
        assert result.done is True
        assert workflow.lookups == 1

        # 同一控制器内重复协调：幂等短路。
        again = await controller.reconcile(_make_ctx("key-1"))
        assert again.done is True
        assert workflow.lookups == 1
        await store.close()

        # 模拟进程重启：新 store（同一 db 文件）+ 新控制器实例。
        restarted_store = ProcessedKeyStore(str(db_file))
        restarted_controller = _controller(workflow, restarted_store)
        after_restart = await restarted_controller.reconcile(_make_ctx("key-1"))
        assert after_restart.done is True
        # 关键断言：重启后不再重复协调（内存 set 方案在此会失效）。
        assert workflow.lookups == 1
        await restarted_store.close()

    _run(_scenario())


@pytest.mark.unit
def test_controller_default_keeps_memory_behavior() -> None:
    """默认 processed_store=None：保持内存 set 行为（向后兼容）。

    同一控制器实例内幂等；新控制器实例（无共享内存）则重新执行——
    与既有 980 项测试所依赖的行为一致。
    """
    workflow = _SucceededWorkflowRuntime()

    async def _scenario() -> None:
        controller = _controller(workflow)
        assert controller._processed_store is None
        first = await controller.reconcile(_make_ctx("key-1"))
        assert first.done is True
        second = await controller.reconcile(_make_ctx("key-1"))
        assert second.done is True
        assert workflow.lookups == 1  # 实例内幂等

        fresh = _controller(workflow)
        third = await fresh.reconcile(_make_ctx("key-1"))
        assert third.done is True
        assert workflow.lookups == 2  # 新实例重走内存路径

    _run(_scenario())
