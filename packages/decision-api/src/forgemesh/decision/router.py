"""ForgeMesh 决策路由与降级（对齐 development-plan 5.5 / architecture 7.5）。

核心原则：

* **决策 ≠ 授权**：Router 仅产出候选，不分配资源、不写资源终态。
* **绝不默认放行**：所有 Provider 失败时返回 ``success=False`` 且
  ``status="FAILED"``，绝不解释为「允许执行默认操作」。
* **高风险不允许通过降级绕过授权或审批**（architecture 7.5）：高风险
  请求的候选来自备用 Provider 时返回 ``status="FALLBACK_REQUIRED"`` 且
  **不设置** ``chosen``，交由人工/上游决策是否审批后重试。
* **可解释**：每次尝试（含超时/异常/空结果/校验失败）均记入 ``attempts``。

降级规则（对齐 5.5「降级决策表」）：

======  ==============================================  ==========
条件     行为                                              是否允许
======  ==============================================  ==========
正常     首选返回有效候选 → 采用（status=OK）                ✅
失败     首选超时/空/校验失败 → 按序尝试下一个（记录）        ✅（非高风险）
高风险   候选来自备用 Provider → status=FALLBACK_REQUIRED，  ✅（但不自动
        不自动采用，交 Policy/审批                            执行）
全失败   返回 status=FAILED，交人工处理                      ✅（禁止默认放行）
======  ==============================================  ==========

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from .circuit_breaker import CircuitBreakerRegistry
from .provider import DecisionCandidate, DecisionProvider, DecisionRequest

#: 路由结果状态三态（对齐 development-plan 5.5 / architecture 7.5）。
#:
#: * ``OK`` —— 首选 Provider 产出有效候选并已采用；
#: * ``FALLBACK_REQUIRED`` —— 高风险请求的候选来自备用 Provider，
#:   **不自动采用**（``chosen=None``），交人工/上游审批后重试；
#: * ``FAILED`` —— 全部 Provider 失败/超时，绝不默认放行。
RouteStatus = Literal["OK", "FALLBACK_REQUIRED", "FAILED"]

# 风险等级排序值（按语义而非字典序）：low < medium < high。
# 与 degradation 模块的 _RISK_ORDER 语义一致；未知等级视为 -1（最低），
# 避免未知等级被误判为高风险而卡死。
_RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _risk_rank(level: str | None) -> int:
    """返回风险等级的排序值；``None`` 或未知等级返回 -1。

    注意不能按字典序比较：Python 中 ``"low" >= "high"`` 为 ``True``，
    会把低风险请求误判为高风险。
    """
    if level is None:
        return -1
    return _RISK_ORDER.get(level, -1)


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
        chosen: 选中的候选（当前策略为取首个有效候选）；无有效候选或
            高风险降级（``FALLBACK_REQUIRED``）时为 ``None``。
        success: 是否成功产出**可自动采用**的候选。``False`` 表示需人工
            处理（含 ``FALLBACK_REQUIRED`` 与 ``FAILED``），**不**代表
            允许默认操作。
        status: 路由状态三态（对齐 development-plan 5.5）——``OK`` /
            ``FALLBACK_REQUIRED`` / ``FAILED``。
        attempts: 按序记录的每次尝试。
        error: 顶层失败原因；``OK`` 与 ``FALLBACK_REQUIRED`` 时为 ``None``。
    """

    candidates: list[DecisionCandidate] = Field(default_factory=list)
    chosen: DecisionCandidate | None = None
    success: bool
    status: RouteStatus
    attempts: list[Attempt] = Field(default_factory=list)
    error: str | None = None


class DecisionRouter:
    """按序降级的多 Provider 决策路由器。

    Args:
        providers: 有序 Provider 列表（首个为主，其余为备用）。
        timeout_seconds: 单个 Provider 的调用时限（秒）；请求携带
            ``deadline_ms`` 时取两者较小值作为本次超时。
        validator: 可选候选校验器；返回 ``True`` 表示候选有效。
            可抛出异常，视为该校验失败并触发降级。
        high_risk_threshold: 视为高风险的等级（按语义排序
            ``low < medium < high``，与字典序无关）。
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
            req: 决策请求；``deadline_ms`` 非 ``None`` 时与全局
                ``timeout_seconds`` 取较小值作为本次超时。

        Returns:
            :class:`DecisionResult`。首选 Provider 成功 → ``status="OK"``；
            高风险请求且候选来自备用 Provider → ``status="FALLBACK_REQUIRED"``
            （候选仍返回但**不设置** ``chosen``，交人工/上游审批后重试）；
            无有效候选 → ``status="FAILED"``（**绝不**默认放行）。
        """
        attempts: list[Attempt] = []

        # 高风险降级保护：高风险请求且无备用 Provider 时，直接返回失败
        high_risk = _risk_rank(req.risk_level) >= _risk_rank(self.high_risk_threshold)
        if high_risk and len(self.providers) < 2:
            return DecisionResult(
                candidates=[],
                chosen=None,
                success=False,
                status="FAILED",
                attempts=[],
                error=(
                    f"high-risk request (risk_level={req.risk_level}) "
                    "requires fallback providers"
                ),
            )

        # per-request 超时：请求时限（毫秒）与全局时限取较小值
        timeout = self.timeout_seconds
        if req.deadline_ms is not None:
            timeout = min(timeout, req.deadline_ms / 1000.0)

        cb = self.circuit_breakers
        for index, provider in enumerate(self.providers):
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
                raw = await asyncio.wait_for(provider.decide(req), timeout=timeout)
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

            # 高风险降级保护（architecture 7.5）：高风险请求的候选来自
            # 备用 Provider 时**不自动采用**——status=FALLBACK_REQUIRED，
            # 不设置 chosen，候选仍返回供人工/上游审批决策。
            if high_risk and index != 0:
                return DecisionResult(
                    candidates=candidates,
                    chosen=None,
                    success=False,
                    status="FALLBACK_REQUIRED",
                    attempts=attempts,
                    error=None,
                )

            return DecisionResult(
                candidates=candidates,
                chosen=candidates[0],
                success=True,
                status="OK",
                attempts=attempts,
                error=None,
            )

        return DecisionResult(
            candidates=[],
            chosen=None,
            success=False,
            status="FAILED",
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
    "RouteStatus",
    "CircuitBreakerRegistry",
]
