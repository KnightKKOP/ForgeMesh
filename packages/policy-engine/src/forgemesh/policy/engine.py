"""ForgeMesh 策略引擎默认实现。

组合授权规则、配额检查与准入钩子，返回三态 PolicyDecision。
对齐 development-plan 5.6 / architecture 4.1、7.5（三态语义恢复：
``degraded`` 降级放行已移除，高风险审批走 ``require_approval``）。

评估流水线：**授权 → 配额 → 高风险审批**，任一 deny 即 deny；
全部通过返回 allow；条件授权/配额接近上限等降级类信息合并进
obligations（不改变 allow 语义），高风险操作返回 require_approval。
"""

from __future__ import annotations

from typing import Any

from .api import PolicyAction, PolicyDecision, PolicyEngine
from .rules import AuthzRule, QuotaRule, RuleSet


class DefaultPolicyEngine(PolicyEngine):
    """默认策略引擎：授权 → 配额 → 准入，任一拒绝即拒绝。

    Args:
        authz_rules: 授权规则列表；为空时所有操作默认拒绝。
        quota_rules: 配额规则列表；为空时跳过配额检查。
        high_risk_actions: 高风险操作集合；命中时返回 require_approval
            （对齐 architecture 14.2 高风险操作清单，审批通过前不放行）。
    """

    def __init__(
        self,
        authz_rules: list[AuthzRule] | None = None,
        quota_rules: list[QuotaRule] | None = None,
        high_risk_actions: set[str] | None = None,
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
        * 高风险操作返回 require_approval（要求人工审批，不放行）；
        * 条件授权/配额接近上限等降级类信息合并进 obligations，返回 allow；
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

        # 3. 高风险操作需要审批（require_approval 是三态之一，不是放行；
        #    architecture 7.5：高风险操作不允许通过降级绕过授权或审批）
        if action.action in self._high_risk_actions:
            return PolicyDecision(
                decision="require_approval",
                reason=f"high-risk action '{action.action}' requires approval",
                obligations=["require_approval"],
            )

        # 4. 条件授权或配额接近上限：降级类信息合并进 obligations，
        #    返回 allow（由执行侧强制满足约束，绝不静默丢弃）
        obligations = authz_result.obligations + quota_result.obligations
        if obligations:
            return PolicyDecision(
                decision="allow",
                reason="quota near limit or authz with conditions",
                obligations=obligations,
            )

        # 5. 全部通过
        return PolicyDecision(
            decision="allow",
            reason="all checks passed",
        )


__all__ = [
    "DefaultPolicyEngine",
]
