"""QoS 单元测试（WP-4.1 多租户配额与 QoS）。

覆盖：
* Priority 枚举排序；
* RateLimiter 令牌桶（允许 / 限流 / 突发）；
* PriorityQueue 优先级排序 + FIFO；
* QoSManager admit（优先级准入 + 限流 + 并发数限制）；
* release_slot 释放并发槽。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.policy.qos import (
    Priority,
    PriorityQueue,
    QoSConfig,
    QoSDecision,
    QoSManager,
    QoSRequest,
    RateLimiter,
)


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


def _req(
    request_id: str,
    tenant_id: str = "team-a",
    priority: Priority = Priority.NORMAL,
) -> QoSRequest:
    """构造测试用 QoSRequest。"""
    return QoSRequest(request_id=request_id, tenant_id=tenant_id, priority=priority)


# ---------------------------------------------------------------------------
# Priority 枚举排序
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_priority_ordering() -> None:
    """CRITICAL < HIGH < NORMAL < LOW（值越小优先级越高）。"""
    assert Priority.CRITICAL < Priority.HIGH < Priority.NORMAL < Priority.LOW
    assert int(Priority.CRITICAL) == 0
    assert int(Priority.HIGH) == 1
    assert int(Priority.NORMAL) == 2
    assert int(Priority.LOW) == 3


@pytest.mark.unit
def test_priority_sortable() -> None:
    """Priority 列表可按值排序。"""
    priorities = [Priority.LOW, Priority.CRITICAL, Priority.NORMAL, Priority.HIGH]
    priorities.sort()
    assert priorities == [
        Priority.CRITICAL,
        Priority.HIGH,
        Priority.NORMAL,
        Priority.LOW,
    ]


# ---------------------------------------------------------------------------
# RateLimiter 令牌桶
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rate_limiter_allows_within_burst() -> None:
    """突发容量内允许（burst=3 → 连续 3 次 acquire 成功）。"""
    limiter = RateLimiter(rate=0.0, burst=3)
    assert _run(limiter.acquire(1)) is True
    assert _run(limiter.acquire(1)) is True
    assert _run(limiter.acquire(1)) is True


@pytest.mark.unit
def test_rate_limiter_denies_when_empty() -> None:
    """令牌耗尽 → False（rate=0 不补充）。"""
    limiter = RateLimiter(rate=0.0, burst=1)
    assert _run(limiter.acquire(1)) is True
    assert _run(limiter.acquire(1)) is False


@pytest.mark.unit
def test_rate_limiter_burst_capacity() -> None:
    """burst=5 → 连续 5 次成功，第 6 次限流。"""
    limiter = RateLimiter(rate=0.0, burst=5)
    for _ in range(5):
        assert _run(limiter.acquire(1)) is True
    assert _run(limiter.acquire(1)) is False


@pytest.mark.unit
def test_rate_limiter_refills_over_time() -> None:
    """有速率时令牌会随时间补充（sleep 后可再次获取）。"""
    limiter = RateLimiter(rate=100.0, burst=1)
    assert _run(limiter.acquire(1)) is True
    assert _run(limiter.acquire(1)) is False  # 瞬间耗尽

    async def wait_and_acquire() -> bool:
        await asyncio.sleep(0.05)  # 0.05s * 100/s = 5 令牌，足够补充
        return await limiter.acquire(1)

    assert _run(wait_and_acquire()) is True


# ---------------------------------------------------------------------------
# PriorityQueue 优先级排序 + FIFO
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_priority_queue_orders_by_priority() -> None:
    """按优先级出队：CRITICAL → HIGH → NORMAL → LOW。"""
    queue = PriorityQueue()
    _run(queue.submit(_req("low", priority=Priority.LOW)))
    _run(queue.submit(_req("crit", priority=Priority.CRITICAL)))
    _run(queue.submit(_req("norm", priority=Priority.NORMAL)))
    _run(queue.submit(_req("high", priority=Priority.HIGH)))

    order: list[str] = []
    while True:
        r = _run(queue.next())
        if r is None:
            break
        order.append(r.request_id)
    assert order == ["crit", "high", "norm", "low"]


@pytest.mark.unit
def test_priority_queue_fifo_same_priority() -> None:
    """同优先级 FIFO：先提交先出队。"""
    queue = PriorityQueue()
    _run(queue.submit(_req("first", priority=Priority.NORMAL)))
    _run(queue.submit(_req("second", priority=Priority.NORMAL)))
    _run(queue.submit(_req("third", priority=Priority.NORMAL)))
    r1 = _run(queue.next())
    r2 = _run(queue.next())
    r3 = _run(queue.next())
    assert r1 is not None and r1.request_id == "first"
    assert r2 is not None and r2.request_id == "second"
    assert r3 is not None and r3.request_id == "third"


@pytest.mark.unit
def test_priority_queue_mixed_priority_and_fifo() -> None:
    """混合优先级：高优先级先出，同优先级 FIFO。"""
    queue = PriorityQueue()
    _run(queue.submit(_req("n1", priority=Priority.NORMAL)))
    _run(queue.submit(_req("h1", priority=Priority.HIGH)))
    _run(queue.submit(_req("n2", priority=Priority.NORMAL)))
    _run(queue.submit(_req("h2", priority=Priority.HIGH)))
    _run(queue.submit(_req("c1", priority=Priority.CRITICAL)))

    order: list[str] = []
    while True:
        r = _run(queue.next())
        if r is None:
            break
        order.append(r.request_id)
    # CRITICAL 先出，然后两个 HIGH（FIFO），然后两个 NORMAL（FIFO）
    assert order == ["c1", "h1", "h2", "n1", "n2"]


@pytest.mark.unit
def test_priority_queue_next_empty_returns_none() -> None:
    """空队列 → None。"""
    queue = PriorityQueue()
    assert _run(queue.next()) is None


@pytest.mark.unit
def test_priority_queue_submit_returns_request_id() -> None:
    """submit 返回 request_id。"""
    queue = PriorityQueue()
    rid = _run(queue.submit(_req("r-123", priority=Priority.NORMAL)))
    assert rid == "r-123"


# ---------------------------------------------------------------------------
# QoSManager admit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_qos_manager_admit_allowed() -> None:
    """无限流无并发压力 → allowed=True, priority 正确返回。"""
    config = QoSConfig(max_concurrent=10)
    manager = QoSManager(config)
    decision = _run(manager.admit(_req("r1", priority=Priority.HIGH)))
    assert decision.allowed is True
    assert decision.reason == "admitted"
    assert decision.priority == Priority.HIGH
    assert decision.queue_position == 0


@pytest.mark.unit
def test_qos_manager_admit_priority_reflected() -> None:
    """不同优先级请求都能准入，decision.priority 反映请求优先级。"""
    config = QoSConfig(max_concurrent=10)
    manager = QoSManager(config)
    for prio in Priority:
        d = _run(manager.admit(_req(f"r-{prio.name}", priority=prio)))
        assert d.allowed is True
        assert d.priority == prio


@pytest.mark.unit
def test_qos_manager_admit_rate_limited() -> None:
    """租户限流 → allowed=False, reason="rate_limited"。"""
    config = QoSConfig(
        max_concurrent=10,
        rate_limit_per_tenant={"team-a": 1},
    )
    manager = QoSManager(config)
    d1 = _run(manager.admit(_req("r1", tenant_id="team-a")))
    assert d1.allowed is True
    # burst=1，第二次限流
    d2 = _run(manager.admit(_req("r2", tenant_id="team-a")))
    assert d2.allowed is False
    assert d2.reason == "rate_limited"


@pytest.mark.unit
def test_qos_manager_admit_no_rate_limit_for_unconfigured_tenant() -> None:
    """未配置限流的租户不受限流影响。"""
    config = QoSConfig(
        max_concurrent=10,
        rate_limit_per_tenant={"team-a": 1},
    )
    manager = QoSManager(config)
    # team-b 未配置限流，连续准入
    for i in range(5):
        d = _run(manager.admit(_req(f"r{i}", tenant_id="team-b")))
        assert d.allowed is True


@pytest.mark.unit
def test_qos_manager_admit_max_concurrent() -> None:
    """全局并发数达上限 → allowed=False, reason="max_concurrent_reached"。"""
    config = QoSConfig(max_concurrent=1)
    manager = QoSManager(config)
    d1 = _run(manager.admit(_req("r1", tenant_id="team-a")))
    assert d1.allowed is True
    d2 = _run(manager.admit(_req("r2", tenant_id="team-b")))
    assert d2.allowed is False
    assert d2.reason == "max_concurrent_reached"


@pytest.mark.unit
def test_qos_manager_max_concurrent_zero() -> None:
    """max_concurrent=0 → 所有请求被拒。"""
    config = QoSConfig(max_concurrent=0)
    manager = QoSManager(config)
    d = _run(manager.admit(_req("r1")))
    assert d.allowed is False
    assert d.reason == "max_concurrent_reached"


# ---------------------------------------------------------------------------
# release_slot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_release_slot_frees_concurrency() -> None:
    """release_slot 后并发槽释放，可重新准入。"""
    config = QoSConfig(max_concurrent=1)
    manager = QoSManager(config)
    d1 = _run(manager.admit(_req("r1", tenant_id="team-a")))
    assert d1.allowed is True
    # 槽位占满
    d2 = _run(manager.admit(_req("r2", tenant_id="team-b")))
    assert d2.allowed is False
    # 释放后可准入
    _run(manager.release_slot("team-a"))
    d3 = _run(manager.admit(_req("r3", tenant_id="team-b")))
    assert d3.allowed is True


@pytest.mark.unit
def test_release_slot_multiple() -> None:
    """多次 admit + release 循环正常工作。"""
    config = QoSConfig(max_concurrent=2)
    manager = QoSManager(config)
    for i in range(10):
        d = _run(manager.admit(_req(f"r{i}", tenant_id="team-a")))
        assert d.allowed is True
        _run(manager.release_slot("team-a"))


@pytest.mark.unit
def test_release_slot_unknown_tenant_silent() -> None:
    """release_slot 未知租户 → 静默忽略，不抛异常。"""
    config = QoSConfig(max_concurrent=10)
    manager = QoSManager(config)
    _run(manager.release_slot("unknown"))  # 不应抛异常


# ---------------------------------------------------------------------------
# QoSConfig + QoSDecision 模型
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_qos_config_defaults() -> None:
    """QoSConfig 默认值合理。"""
    config = QoSConfig()
    assert config.max_concurrent == 100
    assert config.tenant_priorities == {}
    assert config.rate_limit_per_tenant == {}


@pytest.mark.unit
def test_qos_decision_model() -> None:
    """QoSDecision 字段完整。"""
    d = QoSDecision(
        allowed=True,
        reason="admitted",
        priority=Priority.CRITICAL,
        queue_position=0,
    )
    assert d.allowed is True
    assert d.priority == Priority.CRITICAL
    assert d.queue_position == 0
