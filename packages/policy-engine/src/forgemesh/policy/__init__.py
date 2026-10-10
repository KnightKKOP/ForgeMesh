"""ForgeMesh 策略引擎契约（命名空间包 ``forgemesh.policy``）。

本包定义阶段 0 的 Policy 公共契约（对齐 ``development-plan`` 5.6）：

* :class:`PolicyEngine` 负责授权 / 准入 / 配额规则 / 审批要求。

核心原则：**最终授权在 Policy**。JEV（DecisionProvider）的高置信推荐
**不等于**授权；任何分配必须先 Policy 后 Scheduler，跳过即视为治理绕过。
Policy 只回答「是否允许」，不负责具体资源分配。
"""

from __future__ import annotations

from .api import PolicyAction, PolicyDecision, PolicyEngine

__all__ = [
    "PolicyAction",
    "PolicyDecision",
    "PolicyEngine",
]
