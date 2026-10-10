"""Provider 熔断器（对齐 development-plan 5.5）。

三态状态机：

* ``closed``：正常放行，记录失败次数。
* ``open``：拒绝所有请求，等待恢复期后转入 ``half_open``。
* ``half_open``：放行一个探测请求；成功则 ``closed``，失败则 ``open``。

熔断器仅控制「是否放行请求」，**不**改变决策语义：被熔断的 Provider
其请求被跳过并记入 ``attempts``，由 :class:`DecisionRouter` 按序降级到
下一个 Provider。高风险请求的降级保护仍由 Router 与
:mod:`forgemesh.decision.degradation` 强制，熔断器不会绕过授权/审批
（架构不变量 INV-3）。

兼容 Python 3.11+（使用 :class:`enum.StrEnum`）。
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any


class CircuitState(StrEnum):
    """熔断器三态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """单 Provider 熔断器。

    Args:
        failure_threshold: 连续失败次数阈值，达到后熔断。
        recovery_timeout: 熔断后等待恢复的秒数。
        half_open_max_calls: ``half_open`` 状态下允许的探测请求数。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        """当前状态（含惰性状态转移）。"""
        self._maybe_transition_to_half_open()
        return self._state

    def allow_request(self) -> bool:
        """是否允许请求通过。"""
        self._maybe_transition_to_half_open()
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_calls < self._half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False
        return False  # OPEN

    def record_success(self) -> None:
        """记录成功。"""
        if self._state == CircuitState.HALF_OPEN:
            self._reset()
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """记录失败。"""
        self._last_failure_time = time.monotonic()
        if self._state == CircuitState.HALF_OPEN:
            self._trip()
        elif self._state == CircuitState.CLOSED:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._trip()

    def _trip(self) -> None:
        """跳闸到 OPEN。"""
        self._state = CircuitState.OPEN
        self._failure_count = 0
        self._half_open_calls = 0

    def _reset(self) -> None:
        """复位到 CLOSED。"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0

    def _maybe_transition_to_half_open(self) -> None:
        """恢复期到则从 OPEN 惰性转入 HALF_OPEN。"""
        if (
            self._state == CircuitState.OPEN
            and time.monotonic() - self._last_failure_time >= self._recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0

    def stats(self) -> dict[str, Any]:
        """返回熔断器统计信息。"""
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self._failure_threshold,
        }


class CircuitBreakerRegistry:
    """多 Provider 熔断器注册表。

    按 ``provider_id`` 隔离各自独立的 :class:`CircuitBreaker` 实例。
    所有构造参数透传给每个新建的熔断器。

    Args:
        **kwargs: 透传给 :class:`CircuitBreaker` 的构造参数。
    """

    def __init__(self, **kwargs: Any) -> None:
        self._config = kwargs
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, provider_id: str) -> CircuitBreaker:
        """获取（或惰性创建）指定 Provider 的熔断器。"""
        if provider_id not in self._breakers:
            self._breakers[provider_id] = CircuitBreaker(**self._config)
        return self._breakers[provider_id]

    def allow_request(self, provider_id: str) -> bool:
        """查询指定 Provider 是否允许请求通过。"""
        return self.get(provider_id).allow_request()

    def record_success(self, provider_id: str) -> None:
        """记录指定 Provider 一次成功。"""
        self.get(provider_id).record_success()

    def record_failure(self, provider_id: str) -> None:
        """记录指定 Provider 一次失败。"""
        self.get(provider_id).record_failure()


__all__ = [
    "CircuitState",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
]
