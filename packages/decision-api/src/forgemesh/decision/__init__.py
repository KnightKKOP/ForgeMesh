"""ForgeMesh 决策契约（命名空间包 ``forgemesh.decision``）。

本包定义阶段 0 的 JEV（Judgement/Evaluation/Decision）公共契约：

* :mod:`forgemesh.decision.provider` —— 可替换的 :class:`DecisionProvider`。
* :mod:`forgemesh.decision.router`   —— 带超时/校验/**降级**的 :class:`DecisionRouter`。

核心原则：**决策 ≠ 授权**。Router 仅产出候选，绝不分配资源、绝不改写资源终态；
最终授权由 Policy 与 Scheduler 决定。对齐 ``development-plan`` 5.5。
"""

from __future__ import annotations

from .provider import DecisionCandidate, DecisionProvider, DecisionRequest
from .router import Attempt, DecisionResult, DecisionRouter

__all__ = [
    "DecisionRequest",
    "DecisionCandidate",
    "DecisionProvider",
    "Attempt",
    "DecisionResult",
    "DecisionRouter",
]
