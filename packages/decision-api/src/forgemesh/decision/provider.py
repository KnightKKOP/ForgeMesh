"""ForgeMesh 决策 Provider 契约（对齐 development-plan 5.5）。

JEV 只回答「有哪些候选动作」，**不**分配资源、**不**写资源终态
（架构不变量 INV-1 / INV-3）。候选的 ``confidence`` 仅用于排序与解释，
绝不作为授权凭证。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class DecisionRequest(BaseModel):
    """一次决策请求。

    Attributes:
        request_id: 请求唯一标识（用于决策记录回溯，IVT-11）。
        tenant_id: 租户标识（全边界强制隔离）。
        operation: 操作语义标识（如 ``inference.schedule``）。
        context: 决策上下文。生产实现应逐步替换为强类型联合体。
        risk_level: 风险等级（low/medium/high），高风险触发降级保护
            （对齐 development-plan 5.5，默认 ``low``）。
        deadline_ms: 决策时限（毫秒），超时视为失败
            （对齐 development-plan 5.5，默认 2000）。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    tenant_id: str
    operation: str
    context: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high"] = Field(
        default="low", description="风险等级（low/medium/high），高风险触发降级保护"
    )
    deadline_ms: int = Field(default=2000, ge=1, description="决策时限（毫秒），超时视为失败")


class DecisionCandidate(BaseModel):
    """一个候选决策（动作 + 参数 + 解释）。

    Attributes:
        candidate_id: 候选唯一标识。
        action: 候选动作标识。
        parameters: 动作参数。生产实现应强类型化。
        rationale: 可解释理由列表（供审计/解释，不作为授权依据）。
        confidence: 置信度，仅供排序/解释，**绝不**作为授权凭证。
    """

    candidate_id: str
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: list[str] = Field(default_factory=list)
    confidence: float | None = None


class DecisionProvider(Protocol):
    """决策 Provider 协议（结构化子类型，允许任意可替换实现）。

    实现必须提供 ``provider_id`` 与 ``provider_version`` 以支持决策记录
    （IVT-11：记录 provider/version）。``decide`` 返回 0..N 个候选，
    不得改变资源终态。
    """

    provider_id: str
    provider_version: str

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        """返回 0..N 个候选动作；不得改变资源终态（INV-1/INV-3）。"""
        ...


__all__ = [
    "DecisionRequest",
    "DecisionCandidate",
    "DecisionProvider",
]
