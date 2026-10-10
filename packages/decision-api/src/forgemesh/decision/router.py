"""ForgeMesh 决策路由与降级（对齐 development-plan 5.5 / 7.5）。

核心原则：

* **决策 ≠ 授权**：Router 仅产出候选，不分配资源、不写资源终态。
* **绝不默认放行**：所有 Provider 失败时返回 ``success=False``，
  绝不解释为「允许执行默认操作」。
* **可解释**：每次尝试（含超时/异常/空结果/校验失败）均记入 ``attempts``。

降级规则（对齐 5.5「降级决策表」）：

======  ==========================================  ==========
条件     行为                                          是否允许
======  ==========================================  ==========
正常     首选返回有效候选 → 采用                        ✅
失败     首选超时/空/校验失败 → 按序尝试下一个（记录）    ✅
全失败   返回 success=False，交人工处理                  ✅（禁止默认放行）
======  ==========================================  ==========

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from pydantic import BaseModel, Field

from .circuit_breaker import CircuitBreakerRegistry
from .provider import DecisionCandidate, DecisionProvider, DecisionRequest


class Attempt(BaseModel):
    """单次 Provider 尝试记录（用于可解释性与熔断统计）。

    Attributes:
        provider: Provider 标识。
        ok: 本次尝试是否产出有效候选。
        error: 失败原因（超时/异常/空结果/校验失败）；成功时为 ``None``。
        latency_ms: 本次尝试耗时（毫秒）。
    """

    provider: str
    ok: bool
    error: str | None = None
    latency_ms: float = 0.0


class DecisionResult(BaseModel):
    """路由结果。

    Attributes:
        candidates: 筛出的有效候选（可能为空）。
        chosen: 选中的候选（当前策略为取首个有效候选）；无有效候选时为 ``None``。
        success: 是否成功产出候选。``False`` 表示需人工处理，**不**代表允许默认操作。
        attempts: 按序记录的每次尝试。
        error: 顶层失败原因；成功时为 ``None``。
    """

    candidates: list[DecisionCandidate] = Field(default_factory=list)
    chosen: DecisionCandidate | None = None
    success: bool
    attempts: list[Attempt] = Field(default_factory=list)
    error: str | None = None


class DecisionRouter:
    """按序降级的多 Provider 决策路由器。

    Args:
        providers: 有序 Provider 列表（首个为主，其余为备用）。
        timeout_seconds: 单个 Provider 的调用时限（秒）。
        validator: 可选候选校验器；返回 ``True`` 表示候选有效。
            可抛出异常，视为该校验失败并触发降级。
        high_risk_threshold: 视为高风险的等级字符串（按字典序比较）。
        circuit_breakers: 可选的多 Provider 熔断器注册表。提供时，Router
            在调用每个 Provider 前先查询其熔断器是否放行；被熔断的 Provider
            跳过并记入 ``attempts``。成功/失败结果同步回灌熔断器统计。
            熔断器**不**改变降级语义，高风险保护仍由本 Router 强制。
    """

    def __init__(
        self,
        providers: list[DecisionProvider],
        timeout_seconds: float = 5.0,
        validator: Callable[[DecisionCandidate], bool] | None = None,
        high_risk_threshold: str = "high",
        circuit_breakers: CircuitBreakerRegistry | None = None,
    ) -> None:
        self.providers: list[DecisionProvider] = list(providers)
        self.timeout_seconds = timeout_seconds
        self.validator = validator
        self.high_risk_threshold = high_risk_threshold
        self.circuit_breakers = circuit_breakers

    @staticmethod
    def _provider_id(provider: DecisionProvider) -> str:
        """解析 Provider 标识；缺失时回退到类名。"""
        pid = getattr(provider, "provider_id", None)
        if isinstance(pid, str) and pid:
            return pid
        return type(provider).__name__

    async def route(self, req: DecisionRequest) -> DecisionResult:
        """路由一次决策请求，按序降级直至产出有效候选或全部失败。

        Args:
            req: 决策请求。

        Returns:
            :class:`DecisionResult`。若无有效候选，返回 ``success=False``
            （**绝不**默认放行）。
        """
        attempts: list[Attempt] = []

        # 高风险降级保护：高风险请求且无备用 Provider 时，直接返回失败
        risk = getattr(req, "risk_level", None)
        if risk and risk >= self.high_risk_threshold and len(self.providers) < 2:
            return DecisionResult(
                candidates=[],
                chosen=None,
                success=False,
                attempts=[],
                error=f"high-risk request (risk_level={risk}) requires fallback providers",
            )

        cb = self.circuit_breakers
        for provider in self.providers:
            provider_id = self._provider_id(provider)

            # 熔断器：被熔断的 Provider 直接跳过并记录（不改变降级语义）
            if cb is not None and not cb.allow_request(provider_id):
                attempts.append(
                    Attempt(
                        provider=provider_id,
                        ok=False,
                        error="circuit breaker open",
                        latency_ms=0.0,
                    )
                )
                continue

            started = time.monotonic()

            try:
                raw = await asyncio.wait_for(
                    provider.decide(req), timeout=self.timeout_seconds
                )
            except TimeoutError:
                attempts.append(
                    Attempt(
                        provider=provider_id,
                        ok=False,
                        error="TimeoutError",
                        latency_ms=_elapsed_ms(started),
                    )
                )
                if cb is not None:
                    cb.record_failure(provider_id)
                continue
            except Exception as exc:  # noqa: BLE001 - 任意 Provider 故障均降级
                attempts.append(
                    Attempt(
                        provider=provider_id,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        latency_ms=_elapsed_ms(started),
                    )
                )
                if cb is not None:
                    cb.record_failure(provider_id)
                continue

            candidates = list(raw) if raw else []

            if self.validator is not None:
                try:
                    candidates = [c for c in candidates if self.validator(c)]
                except Exception as exc:  # noqa: BLE001 - 校验器故障同样降级
                    attempts.append(
                        Attempt(
                            provider=provider_id,
                            ok=False,
                            error=f"ValidatorError: {exc}",
                            latency_ms=_elapsed_ms(started),
                        )
                    )
                    if cb is not None:
                        cb.record_failure(provider_id)
                    continue

            if not candidates:
                attempts.append(
                    Attempt(
                        provider=provider_id,
                        ok=False,
                        error="no valid candidates",
                        latency_ms=_elapsed_ms(started),
                    )
                )
                if cb is not None:
                    cb.record_failure(provider_id)
                continue

            attempts.append(
                Attempt(
                    provider=provider_id,
                    ok=True,
                    error=None,
                    latency_ms=_elapsed_ms(started),
                )
            )
            if cb is not None:
                cb.record_success(provider_id)
            return DecisionResult(
                candidates=candidates,
                chosen=candidates[0],
                success=True,
                attempts=attempts,
                error=None,
            )

        return DecisionResult(
            candidates=[],
            chosen=None,
            success=False,
            attempts=attempts,
            error="no provider returned a valid candidate",
        )


def _elapsed_ms(started: float) -> float:
    """返回自 ``started``（``time.monotonic``）以来的毫秒数。"""
    return (time.monotonic() - started) * 1000.0


__all__ = [
    "Attempt",
    "DecisionResult",
    "DecisionRouter",
    "CircuitBreakerRegistry",
]
