"""决策请求的风险等级：由服务端按操作推导，客户端只能调高、不能调低。

操作标识即请求的 ``question``，形如 ``<域>.<动作>``（如 ``inference.schedule``）。推导只看
域，即第一个 ``.`` 之前的部分（忽略大小写与首尾空白；不含 ``.`` 时是整个字符串）：

==========================================================  ========
域                                                           等级
==========================================================  ========
``driver`` ``node`` ``mig`` ``gpu`` ``device`` ``plugin``     ``high``
``model`` ``quota`` ``tenant`` ``policy`` ``system``
``platform``（architecture 14.2 列出的高风险操作所在的域）
``inference``                                                ``low``
其他（含自由文本的问题）                                       ``medium``
==========================================================  ========

请求中的 ``riskLevel`` 高于推导值时采用请求值，否则采用推导值。决策路由器的高风险保护以
最终等级为准：高风险请求没有备用 Provider 时直接失败，候选来自备用 Provider 时不自动采用。
"""

from __future__ import annotations

from typing import Literal

RiskLevel = Literal["low", "medium", "high"]

_ORDER: tuple[RiskLevel, ...] = ("low", "medium", "high")

#: 推导为 ``high`` 的域。
HIGH_RISK_DOMAINS = frozenset(
    {
        "driver",
        "node",
        "mig",
        "gpu",
        "device",
        "plugin",
        "model",
        "quota",
        "tenant",
        "policy",
        "system",
        "platform",
    }
)

#: 推导为 ``low`` 的域；不在两个集合中的域为 ``medium``。
LOW_RISK_DOMAINS = frozenset({"inference"})


def derive_risk_level(operation: str) -> RiskLevel:
    """按操作所在的域推导风险等级（规则见模块文档）。"""
    domain = operation.strip().lower().partition(".")[0]
    if domain in HIGH_RISK_DOMAINS:
        return "high"
    if domain in LOW_RISK_DOMAINS:
        return "low"
    return "medium"


def effective_risk_level(operation: str, requested: RiskLevel | None = None) -> RiskLevel:
    """推导值与请求值中较高的一个；没有请求值时就是推导值。"""
    derived = derive_risk_level(operation)
    if requested is None:
        return derived
    return max(derived, requested, key=_ORDER.index)


__all__ = [
    "HIGH_RISK_DOMAINS",
    "LOW_RISK_DOMAINS",
    "RiskLevel",
    "derive_risk_level",
    "effective_risk_level",
]
