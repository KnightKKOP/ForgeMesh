"""JEV（JSON Evaluation）类型化决策 Provider（对齐 development-plan 5.5）。

基于 JSON 表达式做类型化决策：评估每条规则的 JEV 表达式，返回所有匹配候选。
候选的 ``confidence`` 仅用于排序与解释，**绝不**作为授权凭证。

架构不变量：

* **决策 ≠ 授权**（INV-1/INV-3）：仅产出候选，不分配资源、不改写资源终态。
* **可解释**：每条规则携带 ``rationale`` 供审计。

JEV 表达式语法::

    {"field": "x", "op": "eq", "value": 1}      → context["x"] == 1
    {"field": "x", "op": "gt", "value": 1}      → context["x"] > 1
    {"field": "x", "op": "lt", "value": 1}      → context["x"] < 1
    {"field": "x", "op": "in", "value": [1,2]}  → context["x"] in [1,2]
    {"field": "x", "op": "contains", "value": "a"} → "a" in context["x"]
    {"and": [expr1, expr2]}                     → all(expr_i)
    {"or":  [expr1, expr2]}                     → any(expr_i)
    {"not": expr}                               → not expr

兼容 Python 3.9+。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from forgemesh.decision.provider import (
    DecisionCandidate,
    DecisionRequest,
)
from pydantic import BaseModel, ConfigDict, Field

# 支持的叶子操作符
_LEAF_OPS = frozenset({"eq", "gt", "lt", "in", "contains"})


def evaluate_jev(expr: dict[str, Any], context: dict[str, Any]) -> bool:
    """评估一个 JEV 表达式。

    Args:
        expr: JEV 表达式字典。
        context: 决策上下文（``DecisionRequest.context``）。

    Returns:
        表达式求值结果。

    Raises:
        ValueError: 表达式结构非法或操作符未知。
    """
    # 逻辑组合操作符
    if "and" in expr:
        sub_exprs = expr["and"]
        if not isinstance(sub_exprs, list):
            raise ValueError("JEV 'and' expects a list of expressions")
        return all(evaluate_jev(sub, context) for sub in sub_exprs)
    if "or" in expr:
        sub_exprs = expr["or"]
        if not isinstance(sub_exprs, list):
            raise ValueError("JEV 'or' expects a list of expressions")
        return any(evaluate_jev(sub, context) for sub in sub_exprs)
    if "not" in expr:
        return not evaluate_jev(expr["not"], context)

    # 叶子节点：field + op + value
    field = expr.get("field")
    op = expr.get("op")
    value = expr.get("value")
    if field is None or op is None:
        raise ValueError(f"JEV leaf expression missing field/op: {expr!r}")
    if op not in _LEAF_OPS:
        raise ValueError(f"unknown JEV operator: {op!r}")

    actual = context.get(field)

    if op == "eq":
        return actual == value
    if op == "gt":
        return actual is not None and actual > value
    if op == "lt":
        return actual is not None and actual < value
    if op == "in":
        return actual in value
    # contains: value in actual（actual 须为可包含容器，如 list/str/dict）
    return actual is not None and value in actual


class JEVRule(BaseModel):
    """JEV 规则。

    Attributes:
        name: 规则名称（用作候选 ``candidate_id``，应唯一）。
        expression: JEV 表达式字典。
        action: 匹配后产出的候选动作标识。
        parameters: 候选动作参数。
        rationale: 可解释理由列表（供审计，不作为授权依据）。
        confidence: 置信度，仅排序/解释用，**绝不**作为授权凭证。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    expression: dict[str, Any]
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: list[str] = Field(default_factory=list)
    confidence: float | None = None

    def evaluate(self, context: dict[str, Any]) -> bool:
        """在给定上下文上评估本规则的 JEV 表达式。"""
        return evaluate_jev(self.expression, context)

    def to_candidate(self) -> DecisionCandidate:
        """将本规则转换为 :class:`DecisionCandidate`。"""
        return DecisionCandidate(
            candidate_id=self.name,
            action=self.action,
            parameters=self.parameters,
            rationale=self.rationale,
            confidence=self.confidence,
        )


class JEVProvider:
    """JEV 类型化决策 Provider（实现 DecisionProvider 协议）。

    Args:
        rules: 有序 JEV 规则列表。
        provider_id: Provider 标识（默认对齐 manifest ``forgemesh.decision.jev``）。
        provider_version: Provider 版本。
    """

    provider_id: str = "forgemesh.decision.jev"
    provider_version: str = "0.0.1"

    def __init__(
        self,
        rules: list[JEVRule] | None = None,
        *,
        provider_id: str | None = None,
        provider_version: str | None = None,
    ) -> None:
        self.rules: list[JEVRule] = list(rules) if rules else []
        if provider_id is not None:
            self.provider_id = provider_id
        if provider_version is not None:
            self.provider_version = provider_version

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        """评估所有规则，返回匹配候选。

        使用 ``req.context`` 作为 JEV 评估上下文。无匹配时返回空列表。
        """
        return [
            rule.to_candidate() for rule in self.rules if rule.evaluate(req.context)
        ]

    @classmethod
    def from_file(cls, path: str | Path) -> JEVProvider:
        """从 JSON 规则文件加载 Provider。

        文件格式::

            {
              "provider_id": "forgemesh.decision.jev",
              "provider_version": "0.0.1",
              "rules": [
                {"name": "...", "expression": {...}, "action": "...", ...}
              ]
            }

        Args:
            path: JSON 文件路径。

        Returns:
            加载好的 :class:`JEVProvider` 实例。
        """
        p = Path(path)
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        rules = [JEVRule(**r) for r in data.get("rules", [])]
        return cls(
            rules=rules,
            provider_id=data.get("provider_id"),
            provider_version=data.get("provider_version"),
        )


__all__ = [
    "JEVRule",
    "JEVProvider",
    "evaluate_jev",
]
