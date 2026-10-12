"""QuotaPolicy 单元测试（WP-4.1 多租户配额与 QoS）。

覆盖：
* check 允许 / 超配额拒绝；
* consume 原子操作（并发多次消费不超限）；
* release 回收；
* set_limit / get_usage；
* 多租户隔离（不同租户独立配额）。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.policy.quota import QuotaDecision, QuotaLimit, QuotaPolicy


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """为每个测试提供干净的 current event loop。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(coro: Any) -> Any:
    """在当前 event loop 上驱动协程并返回结果。"""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_allowed_within_quota() -> None:
    """配额内 → allowed=True, reason="quota_ok"。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "InferenceService", max=10))
    decision = _run(policy.check("team-a", "InferenceService", requested=1))
    assert decision.allowed is True
    assert decision.reason == "quota_ok"
    assert decision.current == 0
    assert decision.max == 10
    assert decision.requested == 1


@pytest.mark.unit
def test_check_denied_when_exceeded() -> None:
    """超额 → allowed=False, reason="quota_exceeded"。"""
    policy = QuotaPolicy(
        limits={
            "team-a:InferenceService": QuotaLimit(
                resource_type="InferenceService",
                max=10,
                current=10,
                tenant_id="team-a",
            ),
        },
    )
    decision = _run(policy.check("team-a", "InferenceService", requested=1))
    assert decision.allowed is False
    assert decision.reason == "quota_exceeded"
    assert decision.current == 10
    assert decision.max == 10


@pytest.mark.unit
def test_check_allowed_when_no_limit() -> None:
    """无配额记录 → 默认放行, reason="no_quota_limit"。"""
    policy = QuotaPolicy()
    decision = _run(policy.check("team-a", "InferenceService", requested=1))
    assert decision.allowed is True
    assert decision.reason == "no_quota_limit"


# ---------------------------------------------------------------------------
# consume 原子操作
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consume_basic() -> None:
    """单次消费 → allowed=True, current 递增。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "InferenceService", max=10))
    decision = _run(policy.consume("team-a", "InferenceService", 3))
    assert decision.allowed is True
    assert decision.current == 3
    assert decision.reason == "quota_ok"


@pytest.mark.unit
def test_consume_denied_when_exceeded() -> None:
    """消费后超额 → allowed=False, current 不变。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "InferenceService", max=5))
    d1 = _run(policy.consume("team-a", "InferenceService", 5))
    assert d1.allowed is True
    d2 = _run(policy.consume("team-a", "InferenceService", 1))
    assert d2.allowed is False
    assert d2.reason == "quota_exceeded"
    # current 未变
    usage = _run(policy.get_usage("team-a", "InferenceService"))
    assert usage is not None
    assert usage.current == 5


@pytest.mark.unit
def test_consume_atomic_under_concurrency() -> None:
    """并发 20 次 consume(max=10) → 恰好 10 次成功，current=10。"""

    async def scenario() -> tuple[QuotaPolicy, list[QuotaDecision]]:
        policy = QuotaPolicy()
        await policy.set_limit("team-a", "InferenceService", max=10)
        results = await asyncio.gather(
            *[policy.consume("team-a", "InferenceService", 1) for _ in range(20)],
        )
        return policy, list(results)

    policy, results = _run(scenario())
    allowed_count = sum(1 for r in results if r.allowed)
    assert allowed_count == 10
    usage = _run(policy.get_usage("team-a", "InferenceService"))
    assert usage is not None
    assert usage.current == 10


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_release_reclaims_quota() -> None:
    """consume 后 release → current 回降，可再次消费。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "InferenceService", max=10))
    d1 = _run(policy.consume("team-a", "InferenceService", 5))
    assert d1.allowed is True
    assert d1.current == 5
    _run(policy.release("team-a", "InferenceService", 3))
    usage = _run(policy.get_usage("team-a", "InferenceService"))
    assert usage is not None
    assert usage.current == 2
    # 释放后可再次消费至上限
    d2 = _run(policy.consume("team-a", "InferenceService", 8))
    assert d2.allowed is True
    assert d2.current == 10


@pytest.mark.unit
def test_release_floors_at_zero() -> None:
    """release 超过 current → current 下限为 0。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "InferenceService", max=10))
    _run(policy.consume("team-a", "InferenceService", 2))
    _run(policy.release("team-a", "InferenceService", 100))
    usage = _run(policy.get_usage("team-a", "InferenceService"))
    assert usage is not None
    assert usage.current == 0


@pytest.mark.unit
def test_release_unknown_silent() -> None:
    """release 未知租户/资源 → 静默忽略，不抛异常。"""
    policy = QuotaPolicy()
    _run(policy.release("unknown", "resource", 1))  # 不应抛异常


# ---------------------------------------------------------------------------
# set_limit / get_usage
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_set_limit_creates_record() -> None:
    """set_limit 创建记录，current=0。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "GPU", max=8))
    usage = _run(policy.get_usage("team-a", "GPU"))
    assert usage is not None
    assert usage.max == 8
    assert usage.current == 0
    assert usage.tenant_id == "team-a"
    assert usage.resource_type == "GPU"


@pytest.mark.unit
def test_set_limit_updates_existing() -> None:
    """set_limit 对已有记录仅更新上限，保留 current。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "GPU", max=8))
    _run(policy.consume("team-a", "GPU", 3))
    _run(policy.set_limit("team-a", "GPU", max=16))
    usage = _run(policy.get_usage("team-a", "GPU"))
    assert usage is not None
    assert usage.max == 16
    assert usage.current == 3


@pytest.mark.unit
def test_get_usage_returns_none_for_unknown() -> None:
    """未知租户/资源 → None。"""
    policy = QuotaPolicy()
    assert _run(policy.get_usage("unknown", "resource")) is None


@pytest.mark.unit
def test_get_usage_returns_copy() -> None:
    """get_usage 返回副本，修改不影响内部状态。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "GPU", max=8))
    usage = _run(policy.get_usage("team-a", "GPU"))
    assert usage is not None
    usage.current = 999
    usage2 = _run(policy.get_usage("team-a", "GPU"))
    assert usage2 is not None
    assert usage2.current == 0


# ---------------------------------------------------------------------------
# 多租户隔离
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tenant_isolation() -> None:
    """租户 A 的配额消费不影响租户 B（多租户边界强制隔离）。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "InferenceService", max=5))
    _run(policy.set_limit("team-b", "InferenceService", max=5))
    # team-a 消费 5 次 → 耗尽
    for _ in range(5):
        d = _run(policy.consume("team-a", "InferenceService", 1))
        assert d.allowed is True
    # team-a 超额
    d_a = _run(policy.consume("team-a", "InferenceService", 1))
    assert d_a.allowed is False
    # team-b 仍可消费（独立配额）
    d_b = _run(policy.consume("team-b", "InferenceService", 1))
    assert d_b.allowed is True
    # 验证用量隔离
    usage_a = _run(policy.get_usage("team-a", "InferenceService"))
    usage_b = _run(policy.get_usage("team-b", "InferenceService"))
    assert usage_a is not None and usage_a.current == 5
    assert usage_b is not None and usage_b.current == 1


@pytest.mark.unit
def test_tenant_isolation_different_resource_types() -> None:
    """同一租户不同资源类型配额独立。"""
    policy = QuotaPolicy()
    _run(policy.set_limit("team-a", "GPU", max=2))
    _run(policy.set_limit("team-a", "CPU", max=10))
    d_gpu = _run(policy.consume("team-a", "GPU", 2))
    assert d_gpu.allowed is True
    d_cpu = _run(policy.consume("team-a", "CPU", 5))
    assert d_cpu.allowed is True
    d_gpu2 = _run(policy.consume("team-a", "GPU", 1))
    assert d_gpu2.allowed is False
