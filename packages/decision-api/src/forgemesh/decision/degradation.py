"""降级策略（对齐 development-plan 5.5）。

核心规则：

* **低/中风险**：首选失败 → 尝试备用（降级放行）。
* **高风险**：首选失败 → **不**自动降级，交人工处理（防止绕权，
  架构不变量 INV-3）。

降级策略仅回答「是否应尝试备用 Provider」，**不**执行降级、**不**授权、
**不**改写资源终态。最终是否放行仍由 Policy 与 Scheduler 决定。

兼容 Python 3.9+。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# 风险等级排序值（按语义而非字典序）：low < medium < high。
# 未知等级视为 -1（最低），允许降级——避免未知等级被误判为高风险而卡死。
_RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _risk_rank(level: str | None) -> int:
    """返回风险等级的排序值；``None`` 或未知等级返回 -1。"""
    if level is None:
        return -1
    return _RISK_ORDER.get(level, -1)


class DegradationPolicy(BaseModel):
    """降级策略配置。

    Attributes:
        high_risk_level: 视为高风险的等级字符串（按字典序比较）。
        allow_degradation_for_high_risk: 是否允许高风险自动降级（默认
            ``False``，即高风险必须人工处理，防止绕权）。
        max_fallback_attempts: 最大备用尝试次数（含首选），超过即放弃。
    """

    model_config = ConfigDict(extra="forbid")

    high_risk_level: str = "high"
    allow_degradation_for_high_risk: bool = False
    max_fallback_attempts: int = 3


class DegradationStrategy:
    """降级决策器。

    Args:
        policy: 降级策略配置；为 ``None`` 时使用默认策略。
    """

    def __init__(self, policy: DegradationPolicy | None = None) -> None:
        self._policy = policy or DegradationPolicy()

    def should_try_fallback(
        self,
        risk_level: str | None,
        attempt_count: int,
    ) -> bool:
        """是否应该尝试备用 Provider。

        Args:
            risk_level: 请求风险等级（low/medium/high），可为 ``None``。
            attempt_count: 已尝试次数（含首选，从 1 起）。

        Returns:
            是否允许继续降级到下一个备用 Provider。
        """
        # 超过最大尝试次数
        if attempt_count >= self._policy.max_fallback_attempts:
            return False
        # 高风险不自动降级（防止绕权，INV-3）；按语义等级比较而非字典序
        if _risk_rank(risk_level) >= _risk_rank(self._policy.high_risk_level):
            return self._policy.allow_degradation_for_high_risk
        # 低/中风险允许降级
        return True

    def degradation_decision(
        self,
        risk_level: str | None,
        primary_success: bool,
        fallback_available: bool,
    ) -> dict[str, Any]:
        """降级决策结果。

        Args:
            risk_level: 请求风险等级，可为 ``None``。
            primary_success: 主 Provider 是否已成功。
            fallback_available: 是否存在可用的备用 Provider。

        Returns:
            决策字典，``action`` 取值：``use_primary`` / ``degrade`` /
            ``fail``，并附带 ``reason`` 说明。
        """
        if primary_success:
            return {"action": "use_primary", "reason": "primary succeeded"}

        if not fallback_available:
            return {"action": "fail", "reason": "no fallback available"}

        if self.should_try_fallback(risk_level, attempt_count=1):
            return {"action": "degrade", "reason": "fallback to next provider"}
        return {"action": "fail", "reason": "high-risk: degradation not allowed"}


__all__ = [
    "DegradationPolicy",
    "DegradationStrategy",
]
