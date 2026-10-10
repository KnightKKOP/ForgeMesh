"""decision-jev 插件单元测试（对齐 development-plan 5.5）。

覆盖：

* ``JEVRule.evaluate``：eq/gt/lt/in/contains/and/or/not 全操作符。
* ``JEVProvider.decide``：多规则匹配、无匹配返回空、provider_id/version。
* ``JEVProvider.from_file``：从 JSON 文件加载。
* 与 ``DecisionRouter`` 集成。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import pytest
from decision_jev_provider import JEVProvider, JEVRule, evaluate_jev
from forgemesh.decision.provider import (
    DecisionCandidate,
    DecisionRequest,
)
from forgemesh.decision.router import DecisionRouter


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """为每个测试提供干净的 current event loop（兼容 3.9–3.12）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _request(context: dict | None = None) -> DecisionRequest:
    """构造决策请求，context 注入 JEV 评估上下文。"""
    return DecisionRequest(
        request_id="req-1",
        tenant_id="team-a",
        operation="inference.schedule",
        context=context or {},
    )


# --------------------------------------------------------------------------- #
# JEV 表达式评估：叶子操作符
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_jev_eq() -> None:
    ctx = {"x": 5}
    assert evaluate_jev({"field": "x", "op": "eq", "value": 5}, ctx) is True
    assert evaluate_jev({"field": "x", "op": "eq", "value": 6}, ctx) is False


@pytest.mark.unit
def test_jev_gt() -> None:
    ctx = {"x": 5}
    assert evaluate_jev({"field": "x", "op": "gt", "value": 1}, ctx) is True
    assert evaluate_jev({"field": "x", "op": "gt", "value": 5}, ctx) is False
    # 字段缺失时不报错，返回 False
    assert evaluate_jev({"field": "missing", "op": "gt", "value": 1}, ctx) is False


@pytest.mark.unit
def test_jev_lt() -> None:
    ctx = {"x": 5}
    assert evaluate_jev({"field": "x", "op": "lt", "value": 10}, ctx) is True
    assert evaluate_jev({"field": "x", "op": "lt", "value": 5}, ctx) is False


@pytest.mark.unit
def test_jev_in() -> None:
    ctx = {"x": "gpu"}
    assert evaluate_jev({"field": "x", "op": "in", "value": ["gpu", "cpu"]}, ctx) is True
    assert evaluate_jev({"field": "x", "op": "in", "value": ["cpu"]}, ctx) is False


@pytest.mark.unit
def test_jev_contains() -> None:
    ctx = {"tags": ["ai", "inference"]}
    assert evaluate_jev({"field": "tags", "op": "contains", "value": "ai"}, ctx) is True
    assert (
        evaluate_jev({"field": "tags", "op": "contains", "value": "training"}, ctx)
        is False
    )
    # 字段缺失时返回 False
    assert (
        evaluate_jev({"field": "missing", "op": "contains", "value": "a"}, ctx) is False
    )


# --------------------------------------------------------------------------- #
# JEV 表达式评估：逻辑组合操作符
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_jev_and() -> None:
    ctx = {"x": 5}
    expr = {
        "and": [
            {"field": "x", "op": "gt", "value": 1},
            {"field": "x", "op": "lt", "value": 10},
        ]
    }
    assert evaluate_jev(expr, ctx) is True
    expr_false = {
        "and": [
            {"field": "x", "op": "gt", "value": 1},
            {"field": "x", "op": "lt", "value": 3},
        ]
    }
    assert evaluate_jev(expr_false, ctx) is False


@pytest.mark.unit
def test_jev_or() -> None:
    ctx = {"x": 5}
    expr = {
        "or": [
            {"field": "x", "op": "eq", "value": 99},
            {"field": "x", "op": "eq", "value": 5},
        ]
    }
    assert evaluate_jev(expr, ctx) is True
    expr_false = {
        "or": [
            {"field": "x", "op": "eq", "value": 99},
            {"field": "x", "op": "eq", "value": 100},
        ]
    }
    assert evaluate_jev(expr_false, ctx) is False


@pytest.mark.unit
def test_jev_not() -> None:
    ctx = {"x": 5}
    assert evaluate_jev({"not": {"field": "x", "op": "eq", "value": 99}}, ctx) is True
    assert evaluate_jev({"not": {"field": "x", "op": "eq", "value": 5}}, ctx) is False


@pytest.mark.unit
def test_jev_nested_combination() -> None:
    """嵌套组合：and(or(eq, gt), not(lt))。"""
    ctx = {"x": 5, "y": 10}
    expr = {
        "and": [
            {"or": [
                {"field": "x", "op": "eq", "value": 5},
                {"field": "y", "op": "gt", "value": 100},
            ]},
            {"not": {"field": "y", "op": "lt", "value": 1}},
        ]
    }
    assert evaluate_jev(expr, ctx) is True


@pytest.mark.unit
def test_jev_unknown_operator_raises() -> None:
    with pytest.raises(ValueError, match="unknown JEV operator"):
        evaluate_jev({"field": "x", "op": "weird", "value": 1}, {"x": 1})


# --------------------------------------------------------------------------- #
# JEVRule
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_jev_rule_evaluate_and_to_candidate() -> None:
    rule = JEVRule(
        name="high-load-gpu",
        expression={"field": "load", "op": "gt", "value": 0.8},
        action="schedule.gpu.priority",
        parameters={"priority": "high"},
        rationale=["load > 0.8 triggers priority gpu"],
        confidence=0.95,
    )
    assert rule.evaluate({"load": 0.9}) is True
    assert rule.evaluate({"load": 0.5}) is False
    candidate = rule.to_candidate()
    assert candidate.candidate_id == "high-load-gpu"
    assert candidate.action == "schedule.gpu.priority"
    assert candidate.confidence == 0.95


# --------------------------------------------------------------------------- #
# JEVProvider
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_provider_id_and_version_defaults() -> None:
    provider = JEVProvider()
    assert provider.provider_id == "forgemesh.decision.jev"
    assert provider.provider_version == "0.0.1"


@pytest.mark.unit
def test_provider_decide_returns_matching_candidates() -> None:
    provider = JEVProvider(
        rules=[
            JEVRule(
                name="r1",
                expression={"field": "gpu_available", "op": "eq", "value": True},
                action="schedule.gpu",
                confidence=0.9,
            ),
            JEVRule(
                name="r2",
                expression={"field": "load", "op": "gt", "value": 0.9},
                action="schedule.overflow",
                confidence=0.5,
            ),
        ]
    )
    # context: gpu_available=True, load=0.5 → 仅 r1 匹配
    result = list(
        asyncio.get_event_loop().run_until_complete(
            provider.decide(_request({"gpu_available": True, "load": 0.5}))
        )
    )
    assert len(result) == 1
    assert result[0].candidate_id == "r1"

    # context: gpu_available=True, load=0.95 → r1 和 r2 均匹配
    result = list(
        asyncio.get_event_loop().run_until_complete(
            provider.decide(_request({"gpu_available": True, "load": 0.95}))
        )
    )
    assert [c.candidate_id for c in result] == ["r1", "r2"]


@pytest.mark.unit
def test_provider_decide_no_match_returns_empty() -> None:
    provider = JEVProvider(
        rules=[
            JEVRule(
                name="r1",
                expression={"field": "x", "op": "eq", "value": 1},
                action="act-1",
            )
        ]
    )
    result = list(
        asyncio.get_event_loop().run_until_complete(
            provider.decide(_request({"x": 999}))
        )
    )
    assert result == []


# --------------------------------------------------------------------------- #
# JEVProvider.from_file
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_provider_from_file(tmp_path: pytest.fixture) -> None:
    """from_file 应正确加载 JSON 规则文件并保留 provider_id/version。"""
    data = {
        "provider_id": "forgemesh.decision.jev.custom",
        "provider_version": "1.2.3",
        "rules": [
            {
                "name": "rule-a",
                "expression": {"field": "x", "op": "gt", "value": 10},
                "action": "act-a",
                "parameters": {"k": "v"},
                "rationale": ["x > 10"],
                "confidence": 0.7,
            },
            {
                "name": "rule-b",
                "expression": {"and": [
                    {"field": "x", "op": "lt", "value": 100},
                    {"field": "y", "op": "eq", "value": "ready"},
                ]},
                "action": "act-b",
            },
        ],
    }
    fpath = tmp_path / "rules.json"
    fpath.write_text(json.dumps(data), encoding="utf-8")

    provider = JEVProvider.from_file(fpath)
    assert provider.provider_id == "forgemesh.decision.jev.custom"
    assert provider.provider_version == "1.2.3"
    assert len(provider.rules) == 2

    # rule-a: x=20 > 10 → 匹配
    result = list(
        asyncio.get_event_loop().run_until_complete(
            provider.decide(_request({"x": 20, "y": "ready"}))
        )
    )
    assert [c.candidate_id for c in result] == ["rule-a", "rule-b"]

    # rule-a 不匹配: x=5
    result = list(
        asyncio.get_event_loop().run_until_complete(
            provider.decide(_request({"x": 5, "y": "ready"}))
        )
    )
    assert [c.candidate_id for c in result] == ["rule-b"]


# --------------------------------------------------------------------------- #
# 与 DecisionRouter 集成
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_router_integration_primary_success() -> None:
    """JEVProvider 作为主 Provider 时，Router 应路由成功。"""
    provider = JEVProvider(
        rules=[
            JEVRule(
                name="gpu-schedule",
                expression={"field": "gpu_available", "op": "eq", "value": True},
                action="schedule.gpu",
                confidence=0.9,
            )
        ]
    )
    router = DecisionRouter(providers=[provider], timeout_seconds=1.0)
    result = asyncio.get_event_loop().run_until_complete(
        router.route(_request({"gpu_available": True}))
    )
    assert result.success is True
    assert result.chosen is not None
    assert result.chosen.action == "schedule.gpu"


@pytest.mark.unit
def test_router_integration_fallback_when_no_match() -> None:
    """主 JEVProvider 无匹配时应降级到备用 Provider。"""

    class FallbackProvider:
        provider_id = "fallback"
        provider_version = "0.0.1"

        async def decide(
            self, req: DecisionRequest
        ) -> Sequence[DecisionCandidate]:
            return [DecisionCandidate(candidate_id="fb-1", action="schedule.cpu")]

    primary = JEVProvider(
        rules=[
            JEVRule(
                name="r1",
                expression={"field": "x", "op": "eq", "value": 1},
                action="act-1",
            )
        ]
    )
    router = DecisionRouter(
        providers=[primary, FallbackProvider()], timeout_seconds=1.0
    )
    result = asyncio.get_event_loop().run_until_complete(
        router.route(_request({"x": 999}))
    )
    assert result.success is True
    assert result.chosen is not None
    assert result.chosen.candidate_id == "fb-1"
    assert len(result.attempts) == 2
    assert result.attempts[0].ok is False
    assert result.attempts[1].ok is True
