"""ReconciliationController 9 步协调闭环单元测试。

覆盖：幂等去重键、差异计算、Policy 三态（allow/deny/degraded）、
Scheduler 分配成功与失败、幂等性。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.controlplane.reconciler import ReconciliationController, compute_dedup_key
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.reconciler import ReconcileContext
from forgemesh.policy.api import PolicyDecision

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
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(allow)
    )
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
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(deny)
    )
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
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(allow)
    )
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is True
    conditions = result.status_patch["conditions"]
    assert any(c["status"] == "True" for c in conditions)
    assert any(c["reason"] == "Reconciled" for c in conditions)


@pytest.mark.unit
def test_reconcile_policy_degraded():
    """Policy 返回 degraded → done=True, conditions 有 Ready/Unknown。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed)
    degraded = PolicyDecision(decision="degraded", reason="quota near limit")
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(degraded)
    )
    result = asyncio.run(controller.reconcile(ctx))
    assert result.done is True
    conditions = result.status_patch["conditions"]
    assert any(c["status"] == "Unknown" for c in conditions)
    assert any(c["reason"] == "PolicyDegraded" for c in conditions)


@pytest.mark.unit
def test_reconcile_idempotent():
    """同一 dedup_key 第二次调用直接返回 done=True，无副作用。"""
    resource = make_resource(generation=1, observed_generation=0)
    observed = {"phase": "Pending"}
    ctx = make_context(resource, observed, dedup_key="dup-1")
    allow = PolicyDecision(decision="allow", reason="ok")
    controller = ReconciliationController(
        registry=None, policy_engine=MockPolicyEngine(allow)
    )
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
