"""CircuitBreaker / CircuitBreakerRegistry 单元测试（对齐 development-plan 5.5）。

覆盖三态状态机：closed → open → half_open → closed/open，
以及多 Provider 注册表的按 provider_id 隔离。
"""

from __future__ import annotations

import time

import pytest
from forgemesh.decision.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
)


@pytest.mark.unit
def test_circuit_starts3() -> None:
    """初始状态应为 closed。"""
    cb = CircuitBreaker()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


@pytest.mark.unit
def test_circuit_opens_after_threshold() -> None:
    """连续失败达阈值后应转入 open。"""
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


@pytest.mark.unit
def test_circuit_rejects_when_open() -> None:
    """open 状态应拒绝所有请求。"""
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


@pytest.mark.unit
def test_circuit_half_open_after_recovery() -> None:
    """恢复期到后应从 open 惰性转入 half_open。"""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.1)
    assert cb.state == CircuitState.HALF_OPEN


@pytest.mark.unit
def test_circuit_closes_on_half_open_success() -> None:
    """half_open 探测成功应回到 closed。"""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    time.sleep(0.1)
    # 放行一个探测请求
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN
    # 探测成功
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.unit
def test_circuit_reopens_on_half_open_failure() -> None:
    """half_open 探测失败应重新跳闸到 open。"""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
    cb.record_failure()
    time.sleep(0.1)
    assert cb.allow_request() is True
    # 探测失败
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


@pytest.mark.unit
def test_circuit_success_resets_count() -> None:
    """closed 状态下成功应重置失败计数，避免累积误熔断。"""
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.stats()["failure_count"] == 2
    cb.record_success()
    assert cb.stats()["failure_count"] == 0
    # 再次两次失败不应熔断（计数已重置）
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.unit
def test_registry_per_provider() -> None:
    """注册表应按 provider_id 隔离各自独立的熔断器。"""
    reg = CircuitBreakerRegistry(failure_threshold=1)
    # provider-a 一次失败即熔断
    reg.record_failure("provider-a")
    assert reg.allow_request("provider-a") is False
    # provider-b 未失败，仍放行
    assert reg.allow_request("provider-b") is True
    # 两个熔断器是不同实例
    assert reg.get("provider-a") is not reg.get("provider-b")
