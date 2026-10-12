"""DecisionRouter 降级单元测试（对齐 development-plan 5.5 / architecture 7.5）。

覆盖：
* 主 Provider 超时 → 备用 Provider 成功（降级 + attempts 记录）；
* 全部 Provider 失败 → ``success=False`` / ``status="FAILED"``
  （**绝不默认放行**）；
* 高风险 + 备用 Provider 产出候选 → ``status="FALLBACK_REQUIRED"`` 且
  **不设置** ``chosen``（不自动采用，交人工/审批）；
* 高风险 + 首选 Provider 成功 → ``status="OK"`` 正常采用；
* 高风险 + 无备用 Provider → FAILED；
* ``deadline_ms`` 生效：与全局 ``timeout_seconds`` 取较小值。

按要求使用 ``asyncio.get_event_loop().run_until_complete`` 驱动协程。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from forgemesh.decision.provider import (
    DecisionCandidate,
    DecisionProvider,
    DecisionRequest,
)
from forgemesh.decision.router import DecisionRouter


class SlowProvider(DecisionProvider):
    """模拟超时 Provider：睡眠时间大于 Router 时限。"""

    provider_id = "slow"
    provider_version = "0.0.1"

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        await asyncio.sleep(0.5)
        return [
            DecisionCandidate(candidate_id="slow-1", action="schedule.slow")
        ]


class FastProvider(DecisionProvider):
    """模拟正常 Provider：立即返回候选。"""

    provider_id = "fast"
    provider_version = "0.0.1"

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        return [
            DecisionCandidate(candidate_id="fast-1", action="schedule.fast")
        ]


class EmptyProvider(DecisionProvider):
    """模拟返回空候选的 Provider。"""

    provider_id = "empty"
    provider_version = "0.0.1"

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        return []


class BoomProvider(DecisionProvider):
    """模拟抛异常的 Provider。"""

    provider_id = "boom"
    provider_version = "0.0.1"

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        raise RuntimeError("provider exploded")


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


def _request(**kwargs: Any) -> DecisionRequest:
    """构造测试用请求；多余参数透传（如 risk_level / deadline_ms）。"""
    return DecisionRequest(
        request_id="req-1",
        tenant_id="team-a",
        operation="inference.schedule",
        **kwargs,
    )


def test_fallback_when_primary_times_out() -> None:
    """主 Provider 超时后应降级到备用 Provider 并记录两次尝试。"""
    router = DecisionRouter(
        providers=[SlowProvider(), FastProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(router.route(_request()))

    assert result.success is True
    assert result.status == "OK"
    assert result.chosen is not None
    assert result.chosen.candidate_id == "fast-1"
    assert len(result.attempts) == 2
    # 第一次尝试：超时失败
    assert result.attempts[0].provider == "slow"
    assert result.attempts[0].ok is False
    assert result.attempts[0].error == "TimeoutError"
    # 第二次尝试：备用成功
    assert result.attempts[1].provider == "fast"
    assert result.attempts[1].ok is True
    assert result.attempts[1].error is None


def test_all_providers_failed_returns_not_success() -> None:
    """全部 Provider 失败时必须返回 success=False，绝不默认放行。"""
    router = DecisionRouter(
        providers=[BoomProvider(), EmptyProvider(), SlowProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(router.route(_request()))

    assert result.success is False
    assert result.status == "FAILED"
    assert result.chosen is None
    assert result.candidates == []
    assert result.error is not None
    # 三次尝试均被记录
    assert [a.provider for a in result.attempts] == ["boom", "empty", "slow"]
    assert all(a.ok is False for a in result.attempts)
    assert result.attempts[0].error is not None
    assert result.attempts[0].error.startswith("RuntimeError")
    assert result.attempts[1].error == "no valid candidates"


def test_validator_filters_candidates_and_falls_back() -> None:
    """校验器过滤掉全部候选时，应视为失败并继续降级。"""
    router = DecisionRouter(
        providers=[FastProvider(), FastProvider()],
        timeout_seconds=0.05,
        validator=lambda candidate: candidate.candidate_id == "never-matches",
    )

    result = asyncio.get_event_loop().run_until_complete(router.route(_request()))

    assert result.success is False
    assert result.status == "FAILED"
    assert result.chosen is None
    assert all(not a.ok for a in result.attempts)


# ---------------------------------------------------------------------------
# 高风险降级保护（对齐 development-plan 5.5 / architecture 7.5）
# ---------------------------------------------------------------------------


def test_high_risk_fallback_required_when_candidate_from_fallback() -> None:
    """高风险 + 候选来自备用 Provider → FALLBACK_REQUIRED，不自动采用。"""
    router = DecisionRouter(
        providers=[SlowProvider(), FastProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(
        router.route(_request(risk_level="high"))
    )

    # 候选仍返回供人工/上游决策，但不设置 chosen、不自动采用
    assert result.status == "FALLBACK_REQUIRED"
    assert result.success is False
    assert result.chosen is None
    assert [c.candidate_id for c in result.candidates] == ["fast-1"]
    assert result.error is None
    # 可解释性：两次尝试均记录
    assert [a.provider for a in result.attempts] == ["slow", "fast"]
    assert result.attempts[0].ok is False
    assert result.attempts[1].ok is True


def test_high_risk_ok_when_primary_succeeds() -> None:
    """高风险 + 首选 Provider 成功 → OK，正常采用首选候选。"""
    router = DecisionRouter(
        providers=[FastProvider(), FastProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(
        router.route(_request(risk_level="high"))
    )

    assert result.status == "OK"
    assert result.success is True
    assert result.chosen is not None
    assert result.chosen.candidate_id == "fast-1"
    assert len(result.attempts) == 1


def test_high_risk_without_fallback_provider_fails() -> None:
    """高风险 + 无备用 Provider → FAILED（高风险不允许仅靠首选失败后放行）。"""
    router = DecisionRouter(
        providers=[FastProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(
        router.route(_request(risk_level="high"))
    )

    assert result.status == "FAILED"
    assert result.success is False
    assert result.chosen is None
    assert result.candidates == []
    assert "high-risk" in (result.error or "")


def test_low_risk_default_not_treated_as_high_risk() -> None:
    """默认 risk_level="low" 不触发高风险保护（语义排序，非字典序）。"""
    # 单 Provider + 低风险：正常 OK（字典序 "low">="high" 会误判为高风险）
    router = DecisionRouter(
        providers=[FastProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(router.route(_request()))

    assert result.status == "OK"
    assert result.success is True
    assert result.chosen is not None


# ---------------------------------------------------------------------------
# per-request 超时（deadline_ms 与全局 timeout_seconds 取较小值）
# ---------------------------------------------------------------------------


def test_deadline_ms_tighter_than_global_timeout() -> None:
    """deadline_ms(50ms) < 全局(5s) → 按 50ms 超时，首选降级到备用。"""
    router = DecisionRouter(
        providers=[SlowProvider(), FastProvider()],
        timeout_seconds=5.0,
    )

    result = asyncio.get_event_loop().run_until_complete(
        router.route(_request(deadline_ms=50))
    )

    # SlowProvider 睡 0.5s，被 50ms 请求时限打断 → 备用成功
    assert result.status == "OK"
    assert result.chosen is not None
    assert result.chosen.candidate_id == "fast-1"
    assert result.attempts[0].error == "TimeoutError"


def test_deadline_ms_looser_than_global_timeout() -> None:
    """deadline_ms(5000ms) > 全局(50ms) → 按全局 50ms 超时（取较小值）。"""
    router = DecisionRouter(
        providers=[SlowProvider(), FastProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(
        router.route(_request(deadline_ms=5000))
    )

    assert result.status == "OK"
    assert result.chosen is not None
    assert result.chosen.candidate_id == "fast-1"
    assert result.attempts[0].error == "TimeoutError"
