"""策略规则模型：授权规则与配额规则。

对齐 development-plan 5.6 / architecture 4.1；三态语义恢复见
development-plan 5.6 与 architecture 7.5（degraded 降级放行已移除，
条件/告警信息改由 ``obligations`` 表达，高风险审批走 ``require_approval``）。

* :class:`AuthzRule` —— 授权规则，支持无条件 allow / deny 与有条件 allow
  （条件记入 obligations，不改变 allow 语义）。
* :class:`QuotaRule` —— 配额规则，超限 deny、接近上限 allow + 告警约束。
* :class:`RuleSet` —— 规则集合，组合授权与配额检查。

兼容 Python 3.9+。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .api import PolicyAction, PolicyDecision


class AuthzRule(BaseModel):
    """授权规则：允许或拒绝特定 action + tenant 组合。

    当 ``effect="allow"`` 且 ``conditions`` 非空时，表示有条件授权：
    判定结果为 ``allow`` 且条件记入 ``obligations``（对齐
    development-plan 5.6 / architecture 7.5——降级放行态已移除，
    条件约束不改变 allow 语义，由执行侧强制满足）。

    Attributes:
        action: 操作标识（如 ``inference.deploy``）。
        tenant_id: 租户标识；``None`` 表示对所有租户生效。
        effect: 授权效果——``"allow"`` 或 ``"deny"``。
        conditions: 附加约束列表；非空时作为 obligations 附加到 allow 结果。
        reason: 自定义拒绝理由（供审计）。
    """

    model_config = ConfigDict(extra="forbid")

    action: str
    tenant_id: str | None = None  # None 表示对所有租户生效
    effect: str  # "allow" or "deny"
    conditions: list[str] = Field(default_factory=list)
    reason: str = ""


class QuotaRule(BaseModel):
    """配额规则：限制租户的资源数量。

    Attributes:
        tenant_id: 租户标识（全边界强制隔离）。
        resource_kind: 资源类型（如 ``InferenceService``）。
        max_count: 最大允许数量。
        current_count: 当前已用数量。
        warning_threshold: 预警阈值比例（0.0–1.0），默认 0.8。
        reason: 自定义拒绝理由（供审计）。
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    resource_kind: str
    max_count: int = Field(ge=0)
    current_count: int = Field(default=0, ge=0)
    warning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    reason: str = ""


class RuleSet(BaseModel):
    """规则集合：组合授权与配额规则。

    Attributes:
        authz_rules: 授权规则列表。
        quota_rules: 配额规则列表。
    """

    model_config = ConfigDict(extra="forbid")

    authz_rules: list[AuthzRule] = Field(default_factory=list)
    quota_rules: list[QuotaRule] = Field(default_factory=list)

    def check_authz(self, action: PolicyAction) -> PolicyDecision:
        """检查授权规则。默认拒绝（无匹配规则时 deny）。

        匹配优先级：deny 立即生效 > 无条件 allow > 有条件 allow
        （条件记入 obligations）。
        """
        matched_allow = False
        matched_conditional = False
        conditional_obligations: list[str] = []

        for rule in self.authz_rules:
            if rule.action != action.action:
                continue
            if rule.tenant_id is not None and rule.tenant_id != action.tenant_id:
                continue
            if rule.effect == "deny":
                return PolicyDecision(
                    decision="deny",
                    reason=rule.reason
                    or f"action '{action.action}' denied for tenant '{action.tenant_id}'",
                )
            if rule.effect == "allow":
                if rule.conditions:
                    matched_conditional = True
                    conditional_obligations.extend(rule.conditions)
                else:
                    matched_allow = True

        if matched_allow:
            return PolicyDecision(decision="allow", reason="authz rule matched")
        if matched_conditional:
            return PolicyDecision(
                decision="allow",
                reason="authz rule matched with conditions",
                obligations=conditional_obligations,
            )
        # 默认拒绝
        return PolicyDecision(
            decision="deny",
            reason=f"no allow rule matched for action '{action.action}'",
        )

    def check_quota(self, action: PolicyAction) -> PolicyDecision:
        """检查配额规则。超配额 deny，接近上限 allow + 告警约束，否则 allow。"""
        for rule in self.quota_rules:
            if rule.tenant_id != action.tenant_id:
                continue
            resource_kind = action.resource.get("kind", "")
            if rule.resource_kind != resource_kind:
                continue
            if rule.current_count >= rule.max_count:
                return PolicyDecision(
                    decision="deny",
                    reason=rule.reason
                    or f"quota exceeded: {resource_kind} max={rule.max_count}, "
                    f"current={rule.current_count}",
                )
            if rule.current_count >= rule.max_count * rule.warning_threshold:
                return PolicyDecision(
                    decision="allow",
                    reason=f"quota near limit: {resource_kind} "
                    f"current={rule.current_count}, max={rule.max_count}",
                    obligations=["monitor_quota"],
                )
        # 无配额规则匹配，放行
        return PolicyDecision(decision="allow", reason="no quota rule matched")


__all__ = [
    "AuthzRule",
    "QuotaRule",
    "RuleSet",
]
