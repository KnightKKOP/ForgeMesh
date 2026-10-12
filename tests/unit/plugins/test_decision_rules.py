"""decision-rules 插件单元测试（对齐 development-plan 5.5）。

覆盖：

* ``Rule`` 匹配（条件 True/False）与候选转换。
* ``RulesProvider.decide``：多规则匹配、无匹配返回空、provider_id/version。
* 与 ``DecisionRouter`` 集成：主 Provider 返回候选 → 路由成功。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from decision_rules_provider import Rule, RulesProvider
from forgemesh.decision.provider import (
    DecisionCandidate,
    DecisionRequest,
)
from forgemesh.decision.router import DecisionRouter


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


def _request(**overrides: object) -> DecisionRequest:
    """构造决策请求，默认 operation=inference.schedule。"""
    kwargs = {
        "request_id": "req-1",
        "tenant_id": "team-a",
        "operation": "inference.schedule",
    }
    kwargs.update(overrides)  # type: ignore[arg-type]
    return DecisionRequest(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Rule
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_rule_matches_when_condition_true() -> None:
    """条件返回 True 时规则应匹配，且候选字段正确映射。"""
    rule = Rule(
        name="allow-gpu",
        condition=lambda req: req.operation == "inference.schedule",
        action="schedule.gpu",
        parameters={"device": "gpu-0"},
        rationale=["operation is inference.schedule"],
        confidence=0.9,
    )
    req = _request()
    assert rule.matches(req) is True
    candidate = rule.to_candidate()
    assert candidate.candidate_id == "allow-gpu"
    assert candidate.action == "schedule.gpu"
    assert candidate.parameters == {"device": "gpu-0"}
    assert candidate.rationale == ["operation is inference.schedule"]
    assert candidate.confidence == 0.9


@pytest.mark.unit
def test_rule_does_not_match_when_condition_false() -> None:
    """条件返回 False 时规则不应匹配。"""
    rule = Rule(
        name="never",
        condition=lambda req: False,
        action="noop",
    )
    assert rule.matches(_request()) is False


# --------------------------------------------------------------------------- #
# RulesProvider
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_provider_id_and_version_defaults() -> None:
    """RulesProvider 默认 provider_id/version 应对齐 manifest。"""
    provider = RulesProvider()
    assert provider.provider_id == "forgemesh.decision.rules"
    assert provider.provider_version == "0.0.1"


@pytest.mark.unit
def test_provider_decide_returns_all_matching_candidates() -> None:
    """多条规则匹配时应返回全部匹配候选（按序）。"""
    provider = RulesProvider(
        rules=[
            Rule(
                name="r1",
                condition=lambda req: True,
                action="act-1",
                confidence=0.8,
            ),
            Rule(
                name="r2",
                condition=lambda req: req.tenant_id == "team-a",
                action="act-2",
                confidence=0.6,
            ),
            Rule(
                name="r3",
                condition=lambda req: False,
                action="act-3",
            ),
        ]
    )
    result = asyncio.get_event_loop().run_until_complete(provider.decide(_request()))
    assert isinstance(result, Sequence) or hasattr(result, "__iter__")
    candidates = list(result)
    assert len(candidates) == 2
    assert [c.candidate_id for c in candidates] == ["r1", "r2"]
    assert all(isinstance(c, DecisionCandidate) for c in candidates)


@pytest.mark.unit
def test_provider_decide_no_match_returns_empty() -> None:
    """无规则匹配时应返回空列表（非 None）。"""
    provider = RulesProvider(
        rules=[Rule(name="r1", condition=lambda req: False, action="act-1")]
    )
    result = list(
        asyncio.get_event_loop().run_until_complete(provider.decide(_request()))
    )
    assert result == []


@pytest.mark.unit
def test_provider_decide_no_rules_returns_empty() -> None:
    """无规则时 decide 应返回空列表。"""
    provider = RulesProvider()
    result = list(
        asyncio.get_event_loop().run_until_complete(provider.decide(_request()))
    )
    assert result == []


# --------------------------------------------------------------------------- #
# 与 DecisionRouter 集成
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_router_integration_primary_success() -> None:
    """RulesProvider 作为主 Provider 时，Router 应路由成功并选中首个候选。"""
    provider = RulesProvider(
        rules=[
            Rule(
                name="schedule-gpu",
                condition=lambda req: True,
                action="schedule.gpu",
                rationale=["default gpu schedule"],
                confidence=0.9,
            )
        ]
    )
    router = DecisionRouter(providers=[provider], timeout_seconds=1.0)
    result = asyncio.get_event_loop().run_until_complete(router.route(_request()))
    assert result.success is True
    assert result.chosen is not None
    assert result.chosen.action == "schedule.gpu"
    assert len(result.attempts) == 1
    assert result.attempts[0].ok is True


@pytest.mark.unit
def test_router_integration_fallback_when_no_match() -> None:
    """主 RulesProvider 无匹配时应降级到备用 Provider。"""

    class FallbackProvider:
        provider_id = "fallback"
        provider_version = "0.0.1"

        async def decide(
            self, req: DecisionRequest
        ) -> Sequence[DecisionCandidate]:
            return [DecisionCandidate(candidate_id="fb-1", action="schedule.cpu")]

    empty_provider = RulesProvider(
        rules=[Rule(name="r1", condition=lambda req: False, action="act-1")]
    )
    router = DecisionRouter(
        providers=[empty_provider, FallbackProvider()], timeout_seconds=1.0
    )
    result = asyncio.get_event_loop().run_until_complete(router.route(_request()))
    assert result.success is True
    assert result.chosen is not None
    assert result.chosen.candidate_id == "fb-1"
    # 主 Provider 尝试失败 + 备用成功
    assert len(result.attempts) == 2
    assert result.attempts[0].ok is False
    assert result.attempts[1].ok is True
