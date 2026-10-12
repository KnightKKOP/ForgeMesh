"""ReconciliationController 9 步协调闭环单元测试。

覆盖：幂等去重键、差异计算、Policy 三态
（allow/deny/require_approval——degraded 已按 architecture 7.5 移除）、
Scheduler 分配成功与失败、幂等性，以及 Terminating 受控删除闭环
（ReleaseProvider 释放 → 移除 finalizer → 释放 Allocation → 物理删除）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from forgemesh.controlplane.reconciler import (
    ProcessedKeyStore,
    ReconciliationController,
    compute_dedup_key,
)
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.reconciler import ReconcileContext
from forgemesh.policy.api import PolicyDecision
from forgemesh.resource.allocation import Allocation
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

# ---------------------------------------------------------------------- #
# 测试辅助
# ---------------------------------------------------------------------- #


def make_resource(
    kind: str = "InferenceService",
    name: str = "svc-1",
    generation: int = 1,
    observed_generation: int = 0,
    tenant_id: str = "tenant-1",
) -> Resource:
    """构造测试用 Resource。"""
    return Resource(
        kind=kind,
        metadata=ObjectMeta(
            name=name,
            generation=generation,
            observed_generation=observed_generation,
            tenant_id=tenant_id,
        ),
    )


def make_context(
    resource: Resource,
    observed: dict[str, Any] | None = None,
    dedup_key: str = "key-1",
    tenant_id: str = "tenant-1",
    correlation_id: str = "corr-1",
) -> ReconcileContext:
    """构造测试用 ReconcileContext。"""
    return ReconcileContext(
        resource=resource,
        observed=observed or {},
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        dedup_key=dedup_key,
    )


class MockPolicyEngine:
    """Mock PolicyEngine for testing."""

    def __init__(self, result: PolicyDecision) -> None:
        self._result = result

    async def check(self, ctx, action):  # noqa: ANN001
        return self._result


class MockScheduler:
    """Mock Scheduler for testing."""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.allocate_called = False
        self.allocate_count = 0

    async def allocate(self, req):  # noqa: ANN001
        self.allocate_called = True
        self.allocate_count += 1
        if self._fail:
            raise RuntimeError("allocation failed")
        from forgemesh.core.scheduler import AllocationRef

        return AllocationRef(allocation_id="alloc-1", resource_id="node-1")

    async def feasible(self, req):  # noqa: ANN001
        from forgemesh.core.scheduler import FeasibilityResult

        return FeasibilityResult(feasible=True)

    async def release(self, allocation_id: str) -> None:  # noqa: ANN001
        pass


# ---------------------------------------------------------------------- #
# 去重键测试
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_dedup_key_deterministic():
    """相同输入产生相同 dedup_key。"""
    k1 = compute_dedup_key("Node", "default", "node-1", "uid-1", 3, "reconcile")
    k2 = compute_dedup_key("Node", "default", "node-1", "uid-1", 3, "reconcile")
    assert k1.key == k2.key
    assert k1.resource_kind == k2.resource_kind
    assert k1.resource_name == k2.resource_name
    assert k1.generation == k2.generation
    assert k1.operation == k2.operation


@pytest.mark.unit
def test_dedup_key_different_for_different_resources():
    """不同资源产生不同 key。"""
    k1 = compute_dedup_key("Node", "default", "node-1", "uid-1", 3, "reconcile")
    k2 = compute_dedup_key("Node", "default", "node-2", "uid-2", 3, "reconcile")
    assert k1.key != k2.key


# ---------------------------------------------------------------------- #
# 差异计算测试
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_compute_diff_pending_needs_reconcile():
    """phase=Pending → needs_reconcile=True。"""
    resource = make_resource(generation=1, observed_generation=0)
    diff = ReconciliationController._compute_diff(resource, {"phase": "Pending"})
    assert diff["needs_reconcile"] is True
    assert diff["needs_allocation"] is True
    assert diff["needs_workflow"] is True


# ---------------------------------------------------------------------- #
# 协调闭环测试
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_reconcile_no_diff_returns_done():
    """observed 已是 Ready 且 generation 匹配 → done=True。"""
    resource = make_resource(generation=1, observed_generation=1)
    observed = {"phase": "Ready"}
    ctx = make_context(resource, observed)
    allow = PolicyDecision(decision="allow", reason="ok")
    controller = ReconciliationController(registry=None, policy_engine=MockPolicyEngine(allow))
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is True
    assert result.requeue_after is None


@pytest.mark.unit
def test_reconcile_policy_deny_stops():
    """Policy 返回 deny → done=False, requeue_after=30, conditions 有 PolicyDenied。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    deny = PolicyDecision(decision="deny", reason="not allowed")
    controller = ReconciliationController(registry=None, policy_engine=MockPolicyEngine(deny))
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is False
    assert result.requeue_after == 30.0
    conditions = result.status_patch["conditions"]
    assert any(c["reason"] == "PolicyDenied" for c in conditions)
    assert any(c["status"] == "False" for c in conditions)


@pytest.mark.unit
def test_reconcile_policy_allow_proceeds():
    """Policy 返回 allow → done=True, conditions 有 Ready/True。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    allow = PolicyDecision(decision="allow", reason="ok")
    controller = ReconciliationController(registry=None, policy_engine=MockPolicyEngine(allow))
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is True
    conditions = result.status_patch["conditions"]
    assert any(c["status"] == "True" for c in conditions)
    assert any(c["reason"] == "Reconciled" for c in conditions)


@pytest.mark.unit
def test_reconcile_policy_require_approval_blocks():
    """Policy 返回 require_approval → 不产生副作用，requeue 等人工审批。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    approval = PolicyDecision(
        decision="require_approval", reason="high-risk action requires approval"
    )
    scheduler = MockScheduler()
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(approval), scheduler=scheduler
    )
    result = asyncio.run(controller.reconcile(ctx))
    # 审批未通过：不完成、不分配（高风险不允许绕过审批，architecture 7.5）
    assert result.done is False
    assert result.requeue_after == 30.0
    assert scheduler.allocate_called is False
    conditions = result.status_patch["conditions"]
    assert any(c["status"] == "Unknown" for c in conditions)
    assert any(c["reason"] == "PolicyApprovalRequired" for c in conditions)


@pytest.mark.unit
def test_reconcile_idempotent():
    """同一 dedup_key 第二次调用直接返回 done=True，无副作用。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed, dedup_key="dup-1")
    allow = PolicyDecision(decision="allow", reason="ok")
    controller = ReconciliationController(registry=None, policy_engine=MockPolicyEngine(allow))
    # 第一次调用：正常处理
    result1 = asyncio.run(controller.reconcile(ctx))
    assert result1.done is True
    assert result1.status_patch != {}
    # 第二次调用同一 dedup_key：幂等返回，不产生重复副作用
    result2 = asyncio.run(controller.reconcile(ctx))
    assert result2.done is True
    assert result2.requeue_after is None
    assert result2.status_patch == {}


@pytest.mark.unit
def test_reconcile_with_allocation():
    """需要分配且 scheduler 可用 → 调用 scheduler.allocate。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    allow = PolicyDecision(decision="allow", reason="ok")
    scheduler = MockScheduler()
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(allow), scheduler=scheduler
    )
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is True
    assert scheduler.allocate_called is True
    assert scheduler.allocate_count == 1


@pytest.mark.unit
def test_reconcile_allocation_failure():
    """scheduler.allocate 抛异常 → done=False, conditions 有 AllocationFailed。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    allow = PolicyDecision(decision="allow", reason="ok")
    scheduler = MockScheduler(fail=True)
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(allow), scheduler=scheduler
    )
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is False
    assert result.requeue_after == 10.0
    conditions = result.status_patch["conditions"]
    assert any(c["reason"] == "AllocationFailed" for c in conditions)
    assert any(c["status"] == "False" for c in conditions)


class SequencePolicyEngine:
    """按调用顺序依次返回预设决策（最后一个决策重复使用）。"""

    def __init__(self, *decisions: str) -> None:
        self._decisions = list(decisions)
        self.calls = 0

    async def check(self, ctx, action):  # noqa: ANN001
        decision = self._decisions[min(self.calls, len(self._decisions) - 1)]
        self.calls += 1
        return PolicyDecision(decision=decision, reason=f"call-{self.calls}")


class FlakyScheduler(MockScheduler):
    """首次 allocate 失败，之后成功。"""

    async def allocate(self, req):  # noqa: ANN001
        self._fail = self.allocate_count == 0
        return await super().allocate(req)


def _processed_store_variants():
    return [
        pytest.param(lambda: None, id="in-memory-keys"),
        pytest.param(lambda: ProcessedKeyStore(":memory:"), id="processed-store"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("first_decision", ["deny", "require_approval"])
@pytest.mark.parametrize("make_store", _processed_store_variants())
def test_reconcile_requeue_after_policy_block_is_reevaluated(first_decision, make_store):
    """deny / require_approval 未产生副作用：requeue 回来必须重新评估策略。"""
    ctx = make_context(make_resource(generation=1, observed_generation=0), {"phase": "Pending"})
    policy = SequencePolicyEngine(first_decision, "allow")
    scheduler = MockScheduler()
    controller = ReconciliationController(
        registry=None,
        policy_engine=policy,
        scheduler=scheduler,
        processed_store=make_store(),
    )

    blocked = asyncio.run(controller.reconcile(ctx))
    assert blocked.done is False
    assert scheduler.allocate_called is False

    retried = asyncio.run(controller.reconcile(ctx))
    assert policy.calls == 2
    assert retried.done is True
    assert any(c["reason"] == "Reconciled" for c in retried.status_patch["conditions"])
    assert scheduler.allocate_count == 1


@pytest.mark.unit
@pytest.mark.parametrize("make_store", _processed_store_variants())
def test_reconcile_requeue_after_allocation_failure_retries_allocation(make_store):
    """分配失败未产生分配：requeue 回来必须重新尝试分配。"""
    ctx = make_context(make_resource(generation=1, observed_generation=0), {"phase": "Pending"})
    scheduler = FlakyScheduler()
    controller = ReconciliationController(
        registry=None,
        policy_engine=MockPolicyEngine(PolicyDecision(decision="allow", reason="ok")),
        scheduler=scheduler,
        processed_store=make_store(),
    )

    failed = asyncio.run(controller.reconcile(ctx))
    assert failed.done is False

    retried = asyncio.run(controller.reconcile(ctx))
    assert retried.done is True
    assert scheduler.allocate_count == 2

    again = asyncio.run(controller.reconcile(ctx))
    assert again.status_patch == {}
    assert scheduler.allocate_count == 2


# ---------------------------------------------------------------------- #
# Terminating 受控删除闭环（architecture §5.3/§12.4 / development-plan 第 7 章）
# ---------------------------------------------------------------------- #


class StubReleaseProvider:
    """Stub ReleaseProvider：记录调用，可配置抛异常模拟释放失败。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.released: list[str] = []

    async def release(self, resource: Resource) -> None:
        self.released.append(resource.metadata.name)
        if self.fail:
            raise RuntimeError("release failed: gpu driver error")


def make_inference_service(
    name: str = "svc-1",
    tenant_id: str = "tenant-1",
    phase: InferencePhase = InferencePhase.READY,
) -> InferenceService:
    """构造带 spec/status 的 InferenceService（phase 由 status 携带）。"""
    return InferenceService(
        metadata=ObjectMeta(name=name, tenant_id=tenant_id, generation=1),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=phase),
    )


def _terminating_context(
    registry: SQLiteRegistry,
    name: str = "svc-1",
    tenant_id: str = "tenant-1",
    dedup_key: str = "term-1",
) -> ReconcileContext:
    """构造 Terminating 资源的协调上下文（resource 以注册表读回为准注入）。"""
    resource = asyncio.run(registry.get("InferenceService", None, name, tenant_id))
    return ReconcileContext(
        resource=resource,
        observed={"phase": "Terminating"},
        correlation_id="corr-term",
        tenant_id=tenant_id,
        dedup_key=dedup_key,
    )


def _make_controller(
    registry: SQLiteRegistry,
    release_provider: Any | None = None,
) -> ReconciliationController:
    allow = PolicyDecision(decision="allow", reason="ok")
    return ReconciliationController(
        registry=registry,
        policy_engine=MockPolicyEngine(allow),
        release_provider=release_provider,
    )


@pytest.mark.unit
def test_terminating_full_delete_closure():
    """删除闭环：Ready → DELETE(Terminating) → reconcile 释放 → finalizer 清空
    → Allocation Released → 物理删除。"""
    reg = SQLiteRegistry()
    asyncio.run(reg.put(make_inference_service()))
    # 建立关联 Allocation（P1 约定：resource_id = "{kind}/{name}"）
    asyncio.run(
        reg.put_allocation(
            Allocation(
                resource_id="InferenceService/svc-1",
                tenant_id="tenant-1",
                idempotency_key="alloc-key-1",
            )
        )
    )
    # DELETE 受理：Ready 有副作用 → 自动加 finalizer → Terminating
    delete_result = asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))
    assert delete_result.deleted is False

    stub = StubReleaseProvider()
    controller = _make_controller(reg, release_provider=stub)
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    # 释放被调用且仅一次
    assert stub.released == ["svc-1"]
    # 协调完成（物理删除成功）
    assert result.done is True
    assert result.requeue_after is None
    # 资源已物理删除
    with pytest.raises(NotFoundError):
        asyncio.run(reg.get("InferenceService", None, "svc-1", "tenant-1"))
    # Allocation 已释放（Active → Released）
    allocs = asyncio.run(reg.list_allocations("tenant-1"))
    assert len(allocs) == 1
    assert allocs[0]["status"] == "Released"
    assert allocs[0]["released_at"] is not None


@pytest.mark.unit
def test_terminating_release_failure_keeps_terminating():
    """release 抛异常 → 保持 Terminating + requeue 重试（不物理删除）。"""
    reg = SQLiteRegistry()
    asyncio.run(reg.put(make_inference_service()))
    asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))

    stub = StubReleaseProvider(fail=True)
    controller = _make_controller(reg, release_provider=stub)
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    assert result.done is False
    assert result.requeue_after == 10.0
    conditions = result.status_patch["conditions"]
    assert any(c["reason"] == "ReleaseFailed" for c in conditions)
    # 资源仍在且保持 Terminating（不物理删除、GPU 不误删）
    row = reg._conn.execute("SELECT phase FROM resources WHERE name='svc-1'").fetchone()
    assert row is not None and row["phase"] == "Terminating"
    # 失败不登记 dedup_key：同 key 下一轮仍会重试
    assert ctx.dedup_key not in controller._processed_keys


@pytest.mark.unit
def test_terminating_noop_provider_still_deletes():
    """未注入 release_provider（Noop 语义）→ 仅移除 finalizer 后物理删除。"""
    reg = SQLiteRegistry()
    asyncio.run(reg.put(make_inference_service()))
    asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))

    controller = _make_controller(reg)  # release_provider=None
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    assert result.done is True
    row = reg._conn.execute("SELECT phase FROM resources WHERE name='svc-1'").fetchone()
    assert row is None  # 已物理删除


@pytest.mark.unit
def test_terminating_waits_for_other_finalizers():
    """仍有其他 finalizer 持有方 → 移除默认 release finalizer 后等待（requeue）。"""
    reg = SQLiteRegistry()
    svc = make_inference_service()
    svc.finalizers = ["example.com/cleanup", "forgemesh.io/resource-release"]
    asyncio.run(reg.put(svc))
    asyncio.run(reg.request_delete("InferenceService", None, "svc-1", "tenant-1"))

    controller = _make_controller(reg)
    ctx = _terminating_context(reg)

    result = asyncio.run(controller.reconcile(ctx))

    assert result.done is False
    assert result.requeue_after == 5.0
    conditions = result.status_patch["conditions"]
    assert any(c["reason"] == "WaitingFinalizers" for c in conditions)
    # 默认 release finalizer 已被移除，自定义 finalizer 保留
    body = reg._conn.execute("SELECT finalizers FROM resources WHERE name='svc-1'").fetchone()
    assert json.loads(body["finalizers"]) == ["example.com/cleanup"]


@pytest.mark.unit
def test_non_terminating_phase_skips_delete_path():
    """Pending 资源不走删除子流程（走正常差异收敛）。"""
    reg = SQLiteRegistry()
    asyncio.run(reg.put(make_inference_service(phase=InferencePhase.PENDING)))
    allow = PolicyDecision(decision="allow", reason="ok")
    controller = ReconciliationController(registry=reg, policy_engine=MockPolicyEngine(allow))
    resource = make_resource(generation=1, observed_generation=0)
    ctx = make_context(resource, {"phase": "Pending"})
    result = asyncio.run(controller.reconcile(ctx))
    # 正常收敛路径：done=True，资源未被删除
    assert result.done is True
    row = reg._conn.execute("SELECT phase FROM resources WHERE name='svc-1'").fetchone()
    assert row is not None and row["phase"] == "Pending"
