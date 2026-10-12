"""服务优先级与排队（QoS）（对齐 development-plan 5.6 / architecture 4.1）。

提供租户级优先级、令牌桶限流、优先级队列与准入管理。所有限流 / 队列 / 并发
操作通过 :class:`asyncio.Lock` 保护，并发安全。

组件概览：

* :class:`Priority` —— 优先级枚举（值越小优先级越高）。
* :class:`RateLimiter` —— 令牌桶限流器，恒定速率补充令牌，支持突发。
* :class:`PriorityQueue` —— 优先级队列，按 :class:`Priority` 排序、同优先级 FIFO。
* :class:`QoSManager` —— 准入管理器，组合优先级 + 限流 + 全局并发数判断。
"""
from __future__ import annotations

import asyncio
import heapq
import time
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Priority(IntEnum):
    """服务优先级枚举（值越小优先级越高）。

    Attributes:
        CRITICAL: 关键——最高优先级。
        HIGH: 高。
        NORMAL: 普通（默认）。
        LOW: 低。
    """

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class QoSConfig(BaseModel):
    """QoS 配置。

    Attributes:
        tenant_priorities: 租户 → 优先级映射（未配置的租户默认 :attr:`Priority.NORMAL`）。
        max_concurrent: 全局最大并发数（>= 0）。
        rate_limit_per_tenant: 租户 → 每秒令牌数（限流速率）；未配置的租户不限流。
    """

    model_config = ConfigDict(extra="forbid")

    tenant_priorities: dict[str, Priority] = Field(default_factory=dict)
    max_concurrent: int = Field(default=100, ge=0)
    rate_limit_per_tenant: dict[str, int] = Field(default_factory=dict)


class QoSRequest(BaseModel):
    """QoS 请求。

    Attributes:
        request_id: 请求唯一标识。
        tenant_id: 租户标识。
        priority: 优先级（默认 :attr:`Priority.NORMAL`）。
        payload: 请求负载（任意结构）。
        submitted_at: 提交时间戳（单调时钟秒）。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    tenant_id: str
    priority: Priority = Priority.NORMAL
    payload: dict[str, Any] = Field(default_factory=dict)
    submitted_at: float = Field(default_factory=time.monotonic)


class QoSDecision(BaseModel):
    """QoS 准入决策结果。

    Attributes:
        allowed: 是否准入。
        reason: 决策理由——``"admitted"`` / ``"rate_limited"`` /
            ``"max_concurrent_reached"``。
        priority: 请求的优先级。
        queue_position: 队列位置；准入时为 0（立即处理），未入队时为 ``None``。
    """

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    priority: Priority
    queue_position: int | None = None


class RateLimiter:
    """令牌桶限流器。

    以恒定速率 ``rate``（令牌/秒）补充令牌，桶容量为 ``burst``。
    每次 :meth:`acquire` 请求 ``tokens`` 个令牌；桶内不足则限流（返回 ``False``）。

    Args:
        rate: 令牌补充速率（令牌/秒）。
        burst: 桶容量（最大突发令牌数）。
    """

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens: float = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """根据已流逝时间补充令牌（调用前须持锁）。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last_refill = now

    async def acquire(self, tokens: int = 1) -> bool:
        """获取令牌。

        Args:
            tokens: 需要的令牌数。

        Returns:
            ``True`` 表示获取成功（已扣减令牌）；``False`` 表示令牌不足被限流。
        """
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class PriorityQueue:
    """优先级队列：按 :class:`Priority` 排序，同优先级 FIFO。

    内部以 ``(priority_value, sequence, request)`` 三元组维护最小堆，
    ``sequence`` 单调递增以保证同优先级 FIFO。所有操作通过
    :class:`asyncio.Lock` 保护。
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, QoSRequest]] = []
        self._seq: int = 0
        self._lock = asyncio.Lock()

    async def submit(self, request: QoSRequest) -> str:
        """提交请求，返回 ``request_id``。

        Args:
            request: 待入队请求。

        Returns:
            ``request.request_id``。
        """
        async with self._lock:
            self._seq += 1
            heapq.heappush(self._heap, (int(request.priority), self._seq, request))
            return request.request_id

    async def next(self) -> QoSRequest | None:
        """取出最高优先级请求（队列为空返回 ``None``）。"""
        async with self._lock:
            if not self._heap:
                return None
            _, _, request = heapq.heappop(self._heap)
            return request


class QoSManager:
    """QoS 管理器：组合优先级、限流与全局并发数准入判断。

    准入顺序（均在管理器锁内串行执行，避免 check-then-act 竞态）：

    1. 全局并发数达上限 → 拒绝（``reason="max_concurrent_reached"``）；
    2. 租户限流未通过 → 拒绝（``reason="rate_limited"``）；
    3. 通过 → 占用并发槽，返回 ``allowed=True``。

    Args:
        config: QoS 配置。
    """

    def __init__(self, config: QoSConfig) -> None:
        self._config = config
        self._rate_limiters: dict[str, RateLimiter] = {}
        for tenant_id, rate in config.rate_limit_per_tenant.items():
            self._rate_limiters[tenant_id] = RateLimiter(rate=float(rate), burst=rate)
        self._concurrent: dict[str, int] = {}
        self._total_concurrent: int = 0
        self._lock = asyncio.Lock()

    def _resolve_priority(self, tenant_id: str) -> Priority:
        """解析租户优先级（未配置则默认 NORMAL）。"""
        return self._config.tenant_priorities.get(tenant_id, Priority.NORMAL)

    async def admit(self, request: QoSRequest) -> QoSDecision:
        """准入判断：优先级 + 限流 + 并发数。

        Args:
            request: 待准入请求。

        Returns:
            :class:`QoSDecision`。
        """
        priority = request.priority
        async with self._lock:
            # 1. 全局并发上限
            if self._total_concurrent >= self._config.max_concurrent:
                return QoSDecision(
                    allowed=False,
                    reason="max_concurrent_reached",
                    priority=priority,
                    queue_position=None,
                )
            # 2. 租户限流（限流器持自身锁，与管理器锁单向嵌套，无死锁风险）
            limiter = self._rate_limiters.get(request.tenant_id)
            if limiter is not None:
                allowed = await limiter.acquire()
                if not allowed:
                    return QoSDecision(
                        allowed=False,
                        reason="rate_limited",
                        priority=priority,
                        queue_position=None,
                    )
            # 3. 占用并发槽
            self._total_concurrent += 1
            self._concurrent[request.tenant_id] = (
                self._concurrent.get(request.tenant_id, 0) + 1
            )
            return QoSDecision(
                allowed=True,
                reason="admitted",
                priority=priority,
                queue_position=0,
            )

    async def release_slot(self, tenant_id: str) -> None:
        """释放并发槽。

        递减全局与租户级并发计数，下限为 0。

        Args:
            tenant_id: 租户标识。
        """
        async with self._lock:
            if self._total_concurrent > 0:
                self._total_concurrent -= 1
            current = self._concurrent.get(tenant_id, 0)
            if current > 0:
                self._concurrent[tenant_id] = current - 1


__all__ = [
    "Priority",
    "QoSConfig",
    "QoSRequest",
    "QoSDecision",
    "RateLimiter",
    "PriorityQueue",
    "QoSManager",
]
