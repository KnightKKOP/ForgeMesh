"""DegradationStrategy / DegradationPolicy 单元测试（对齐 development-plan 5.5）。

覆盖降级核心规则：

* 低/中风险：首选失败 → 尝试备用（降级放行）。
* 高风险：首选失败 → 不自动降级（防止绕权，INV-3）。
* 超过最大尝试次数 → 不再降级。
* 主 Provider 成功 → 不降级。
* 无备用 Provider → fail。
"""

from __future__ import annotations

import pytest
from forgemesh.decision.degradation import DegradationPolicy, DegradationStrategy


@pytest.mark.unit
def test_low_risk_allow_fallback() -> None:
    """低/中风险应允许降级到备用 Provider。"""
    strategy = DegradationStrategy()
    assert strategy.should_try_fallback("low", 1) is True
    assert strategy.should_try_fallback("medium", 1) is True
    # 风险等级为 None 同样允许
    assert strategy.should_try_fallback(None, 1) is True


@pytest.mark.unit
def test_high_risk_no_fallback() -> None:
    """高风险默认不自动降级（防止绕权，INV-3）。"""
    strategy = DegradationStrategy()
    assert strategy.should_try_fallback("high", 1) is False


@pytest.mark.unit
def test_high_risk_with_override() -> None:
    """allow_degradation_for_high_risk=True 时高风险允许降级。"""
    policy = DegradationPolicy(allow_degradation_for_high_risk=True)
    strategy = DegradationStrategy(policy)
    assert strategy.should_try_fallback("high", 1) is True


@pytest.mark.unit
def test_max_attempts_reached() -> None:
    """超过最大尝试次数应不再降级。"""
    policy = DegradationPolicy(max_fallback_attempts=2)
    strategy = DegradationStrategy(policy)
    # 第 1 次尝试：允许
    assert strategy.should_try_fallback("low", 1) is True
    # 达到上限：拒绝
    assert strategy.should_try_fallback("low", 2) is False
    # 超过上限：拒绝
    assert strategy.should_try_fallback("low", 3) is False


@pytest.mark.unit
def test_primary_success_no_degradation() -> None:
    """主 Provider 成功时不降级，直接采用主结果。"""
    strategy = DegradationStrategy()
    decision = strategy.degradation_decision(
        risk_level="low",
        primary_success=True,
        fallback_available=True,
    )
    assert decision["action"] == "use_primary"
    assert decision["reason"] == "primary succeeded"


@pytest.mark.unit
def test_no_fallback_available() -> None:
    """无备用 Provider 且主失败时应 fail。"""
    strategy = DegradationStrategy()
    decision = strategy.degradation_decision(
        risk_level="low",
        primary_success=False,
        fallback_available=False,
    )
    assert decision["action"] == "fail"
    assert decision["reason"] == "no fallback available"


@pytest.mark.unit
def test_high_risk_degradation_decision_fails() -> None:
    """高风险主失败且有备用时，degradation_decision 应 fail 而非 degrade。"""
    strategy = DegradationStrategy()
    decision = strategy.degradation_decision(
        risk_level="high",
        primary_success=False,
        fallback_available=True,
    )
    assert decision["action"] == "fail"
    assert "high-risk" in decision["reason"]
