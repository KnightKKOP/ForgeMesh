"""DecisionRouter 降级单元测试（对齐 development-plan 5.5 / 第 8 章 8.1）。

覆盖：
* 主 Provider 超时 → 备用 Provider 成功（降级 + attempts 记录）；
* 全部 Provider 失败 → ``success=False``（**绝不默认放行**）。

按要求使用 ``asyncio.get_event_loop().run_until_complete`` 驱动协程。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
from typing import Sequence

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
    """为每个测试提供干净的 current event loop（兼容 3.9–3.12）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _request() -> DecisionRequest:
    return DecisionRequest(
        request_id="req-1",
        tenant_id="team-a",
        operation="inference.schedule",
    )


def test_fallback_when_primary_times_out() -> None:
    """主 Provider 超时后应降级到备用 Provider 并记录两次尝试。"""
    router = DecisionRouter(
        providers=[SlowProvider(), FastProvider()],
        timeout_seconds=0.05,
    )

    result = asyncio.get_event_loop().run_until_complete(router.route(_request()))

    assert result.success is True
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
    assert result.chosen is None
    assert all(not a.ok for a in result.attempts)
