"""确定性规则决策 Provider（对齐 development-plan 5.5）。

基于预定义规则集做确定性决策：按规则顺序匹配，返回**所有**匹配候选。
候选的 ``confidence`` 仅用于排序与解释，**绝不**作为授权凭证。

架构不变量：

* **决策 ≠ 授权**（INV-1/INV-3）：仅产出候选，不分配资源、不改写资源终态。
* **可解释**：每条规则携带 ``rationale`` 供审计。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from forgemesh.decision.provider import (
    DecisionCandidate,
    DecisionRequest,
)
from pydantic import BaseModel, ConfigDict, Field


class Rule(BaseModel):
    """单条决策规则。

    Attributes:
        name: 规则名称（用作候选 ``candidate_id``，应唯一）。
        condition: 匹配条件函数，接收 :class:`DecisionRequest` 返回 ``bool``。
        action: 匹配后产出的候选动作标识。
        parameters: 候选动作参数。
        rationale: 可解释理由列表（供审计，不作为授权依据）。
        confidence: 置信度，仅排序/解释用，**绝不**作为授权凭证。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    condition: Callable[[DecisionRequest], bool]
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: list[str] = Field(default_factory=list)
    confidence: float | None = None

    def matches(self, req: DecisionRequest) -> bool:
        """评估规则是否匹配请求。"""
        return self.condition(req)

    def to_candidate(self) -> DecisionCandidate:
        """将本规则转换为 :class:`DecisionCandidate`。"""
        return DecisionCandidate(
            candidate_id=self.name,
            action=self.action,
            parameters=self.parameters,
            rationale=self.rationale,
            confidence=self.confidence,
        )


class RulesProvider:
    """确定性规则决策 Provider（实现 DecisionProvider 协议）。

    Args:
        rules: 有序规则列表；按序匹配，返回所有匹配候选。
        provider_id: Provider 标识（默认对齐 manifest ``forgemesh.decision.rules``）。
        provider_version: Provider 版本。
    """

    provider_id: str = "forgemesh.decision.rules"
    provider_version: str = "0.0.1"

    def __init__(
        self,
        rules: list[Rule] | None = None,
        *,
        provider_id: str | None = None,
        provider_version: str | None = None,
    ) -> None:
        self.rules: list[Rule] = list(rules) if rules else []
        if provider_id is not None:
            self.provider_id = provider_id
        if provider_version is not None:
            self.provider_version = provider_version

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        """按规则顺序匹配，返回所有匹配候选。

        无匹配规则时返回空列表（Router 将按降级策略处理）。
        """
        return [rule.to_candidate() for rule in self.rules if rule.matches(req)]


__all__ = [
    "Rule",
    "RulesProvider",
]
