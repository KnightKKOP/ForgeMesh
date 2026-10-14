"""DefaultPolicyEngine 单元测试（对齐 development-plan 5.6 / architecture 4.1、7.5）。

覆盖：
* 授权通过 → allow；
* 授权拒绝 → deny；
* 无匹配规则 → deny（默认拒绝）；
* 配额超限 → deny；
* 配额接近上限 → allow + monitor_quota 约束；
* 高风险操作 → require_approval（**不是放行**）；
* 租户隔离：租户 A 的规则不影响租户 B；
* 有条件授权 → allow + 条件约束；
* PolicyDecision 的 policy_id / approvers 审计字段。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.policy.api import PolicyAction, PolicyDecision
from forgemesh.policy.engine import DefaultPolicyEngine
from forgemesh.policy.rules import AuthzRule, QuotaRule
from pydantic import ValidationError


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


def _action(
    action: str,
    tenant_id: str = "team-a",
    kind: str = "InferenceService",
) -> PolicyAction:
    """构造测试用 PolicyAction。"""
    return PolicyAction(action=action, tenant_id=tenant_id, resource={"kind": kind})


# ---------------------------------------------------------------------------
# 授权检查
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_allow_when_authz_passes() -> None:
    """有 allow 规则且无配额违规 → decision="allow"。"""
    engine = DefaultPolicyEngine(
        authz_rules=[
            AuthzRule(action="inference.deploy", tenant_id="team-a", effect="allow"),
        ],
    )
    result = _run(engine.check(ctx=None, action=_action("inference.deploy")))
    assert result.decision == "allow"
    assert result.reason == "all checks passed"


@pytest.mark.unit
def test_deny_when_authz_denies() -> None:
    """有 deny 规则 → decision="deny"。"""
    engine = DefaultPolicyEngine(
        authz_rules=[
            AuthzRule(action="inference.deploy", tenant_id="team-a", effect="deny"),
        ],
    )
    result = _run(engine.check(ctx=None, action=_action("inference.deploy")))
    assert result.decision == "deny"


@pytest.mark.unit
def test_deny_by_default() -> None:
    """无匹配规则 → decision="deny"（默认拒绝，绝不默认放行）。"""
    engine = DefaultPolicyEngine()
    result = _run(engine.check(ctx=None, action=_action("inference.deploy")))
    assert result.decision == "deny"
    assert "no allow rule matched" in result.reason


# ---------------------------------------------------------------------------
# 配额检查
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deny_when_quota_exceeded() -> None:
    """current_count >= max_count → decision="deny"。"""
    engine = DefaultPolicyEngine(
        authz_rules=[
            AuthzRule(action="inference.deploy", tenant_id="team-a", effect="allow"),
        ],
        quota_rules=[
            QuotaRule(
                tenant_id="team-a",
                resource_kind="InferenceService",
                max_count=10,
                current_count=10,
            ),
        ],
    )
    result = _run(engine.check(ctx=None, action=_action("inference.deploy")))
    assert result.decision == "deny"


@pytest.mark.unit
def test_allow_with_obligations_when_quota_near_limit() -> None:
    """current_count >= max_count * 0.8 → allow，obligations 携带 monitor_quota。"""
    engine = DefaultPolicyEngine(
        authz_rules=[
            AuthzRule(action="inference.deploy", tenant_id="team-a", effect="allow"),
        ],
        quota_rules=[
            QuotaRule(
                tenant_id="team-a",
                resource_kind="InferenceService",
                max_count=10,
                current_count=8,
            ),
        ],
    )
    result = _run(engine.check(ctx=None, action=_action("inference.deploy")))
    assert result.decision == "allow"
    assert "monitor_quota" in result.obligations
    assert "quota near limit" in result.reason


# ---------------------------------------------------------------------------
# 高风险审批
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_high_risk_requires_approval() -> None:
    """高风险 action → decision="require_approval"，且不是放行（≠ allow）。"""
    engine = DefaultPolicyEngine(
        authz_rules=[
            AuthzRule(action="node.delete", tenant_id="team-a", effect="allow"),
        ],
        high_risk_actions={"node.delete"},
    )
    result = _run(engine.check(ctx=None, action=_action("node.delete")))
    assert result.decision == "require_approval"
    # require_approval 是独立的审批态：既非 allow 也非 deny，绝不静默放行
    assert result.decision != "allow"
    assert "require_approval" in result.obligations
    assert "requires approval" in result.reason


# ---------------------------------------------------------------------------
# 租户隔离
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tenant_isolation_in_authz() -> None:
    """租户 A 的规则不影响租户 B（多租户边界强制隔离）。"""
    engine = DefaultPolicyEngine(
        authz_rules=[
            AuthzRule(action="inference.deploy", tenant_id="team-a", effect="allow"),
        ],
    )
    # team-a 有 allow 规则 → allow
    result_a = _run(engine.check(ctx=None, action=_action("inference.deploy", tenant_id="team-a")))
    assert result_a.decision == "allow"
    # team-b 无匹配规则 → 默认拒绝
    result_b = _run(engine.check(ctx=None, action=_action("inference.deploy", tenant_id="team-b")))
    assert result_b.decision == "deny"


# ---------------------------------------------------------------------------
# 有条件授权降级
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_allow_with_obligations_when_authz_conditioned() -> None:
    """授权有条件时 → allow，obligations 携带条件约束（不放行语义不变）。"""
    engine = DefaultPolicyEngine(
        authz_rules=[
            AuthzRule(
                action="inference.deploy",
                tenant_id="team-a",
                effect="allow",
                conditions=["require_mfa"],
            ),
        ],
    )
    result = _run(engine.check(ctx=None, action=_action("inference.deploy")))
    assert result.decision == "allow"
    assert "require_mfa" in result.obligations


# ---------------------------------------------------------------------------
# 审计字段（development-plan 5.6：policy_id / approvers）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_policy_decision_audit_fields_passthrough() -> None:
    """PolicyDecision 的 policy_id / approvers 字段可构造且默认为 None。"""
    # 默认值：不参与判定，仅审计追溯
    default_decision = PolicyDecision(decision="allow", reason="ok")
    assert default_decision.policy_id is None
    assert default_decision.approvers is None
    # require_approval 场景：携带审批人与策略标识
    decision = PolicyDecision(
        decision="require_approval",
        reason="high-risk action requires approval",
        obligations=["require_approval"],
        policy_id="policy/node-delete-high-risk",
        approvers=["alice@example.com", "bob@example.com"],
    )
    assert decision.policy_id == "policy/node-delete-high-risk"
    assert decision.approvers == ["alice@example.com", "bob@example.com"]


@pytest.mark.unit
def test_degraded_decision_type_rejected() -> None:
    """degraded 已从决策三态移除，构造必须被拒绝（架构 7.5：禁止降级放行）。"""
    with pytest.raises(ValidationError):
        PolicyDecision(decision="degraded")
