"""ForgeMesh 策略引擎默认实现。

组合授权规则、配额检查与准入钩子，返回三态 PolicyDecision。
对齐 development-plan 5.6 / architecture 4.1。

评估流水线：**授权 → 配额 → 高风险审批**，任一 deny 即 deny；
全部通过返回 allow；配额接近上限或授权带条件返回 degraded。

兼容 Python 3.9+。
"""
from __future__ import annotations

from typing import Any, List, Optional, Set

from .api import PolicyAction, PolicyDecision, PolicyEngine
from .rules import AuthzRule, QuotaRule, RuleSet


class DefaultPolicyEngine(PolicyEngine):
    """默认策略引擎：授权 → 配额 → 准入，任一拒绝即拒绝。

    Args:
        authz_rules: 授权规则列表；为空时所有操作默认拒绝。
        quota_rules: 配额规则列表；为空时跳过配额检查。
        high_risk_actions: 高风险操作集合；命中时返回 degraded 并要求审批。
    """

    def __init__(
        self,
        authz_rules: Optional[List[AuthzRule]] = None,
        quota_rules: Optional[List[QuotaRule]] = None,
        high_risk_actions: Optional[Set[str]] = None,
    ) -> None:
        self._rule_set = RuleSet(
            authz_rules=authz_rules or [],
            quota_rules=quota_rules or [],
        )
        self._high_risk_actions = high_risk_actions or set()

    async def check(self, ctx: Any, action: PolicyAction) -> PolicyDecision:
        """评估操作是否被允许。

        评估顺序：授权 → 配额 → 高风险审批。

        * 任一环节 deny 即返回 deny；
        * 高风险操作返回 degraded（要求审批）；
        * 授权带条件或配额接近上限返回 degraded；
        * 全部通过返回 allow。

        Args:
            ctx: 调用上下文（如 actor、correlation_id、审批上下文等）。
            action: 待鉴权操作。

        Returns:
            三态判定 :class:`PolicyDecision`。**默认拒绝**：无法判定时返回 deny。
        """
        # 1. 授权检查
        authz_result = self._rule_set.check_authz(action)
        if authz_result.decision == "deny":
            return authz_result

        # 2. 配额检查
        quota_result = self._rule_set.check_quota(action)
        if quota_result.decision == "deny":
            return quota_result

        # 3. 高风险操作需要审批
        if action.action in self._high_risk_actions:
            return PolicyDecision(
                decision="degraded",
                reason=f"high-risk action '{action.action}' requires approval",
                obligations=["require_approval"],
            )

        # 4. 授权或配额处于降级态
        if authz_result.decision == "degraded" or quota_result.decision == "degraded":
            return PolicyDecision(
                decision="degraded",
                reason="quota near limit or authz with conditions",
                obligations=authz_result.obligations + quota_result.obligations,
            )

        # 5. 全部通过
        return PolicyDecision(
            decision="allow",
            reason="all checks passed",
        )


__all__ = [
    "DefaultPolicyEngine",
]
