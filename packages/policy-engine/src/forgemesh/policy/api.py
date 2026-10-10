"""ForgeMesh 策略引擎接口（对齐 development-plan 5.6 / architecture 4.1）。

Policy 负责授权、准入、配额规则与审批要求，**最终授权在 Policy**：

* JEV（DecisionProvider）的高置信推荐**不等于**授权（INV-3）。
* Policy 只回答「是否允许」，不负责具体资源分配（分配由 Scheduler 唯一负责）。
* 任何分配必须先 Policy 后 Scheduler；跳过即抛治理绕过错误（编排层强制）。

兼容 Python 3.9+。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PolicyAction(BaseModel):
    """待鉴权的操作。

    Attributes:
        action: 操作标识（如 ``inference.deploy``）。
        tenant_id: 租户标识（全边界强制隔离）。
        resource: 目标资源描述（可选）。
    """

    action: str
    tenant_id: str
    resource: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    """策略判定结果（三态：allow / deny / degraded）。

    Attributes:
        decision: 判定结论——``allow``（允许）、``deny``（拒绝）、``degraded``（降级放行，附带约束）。
        reason: 判定理由（供审计/解释）。
        obligations: 附加约束（如「必须记录审计」）。
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["allow", "deny", "degraded"]
    reason: str = ""
    obligations: list[str] = Field(default_factory=list)


class PolicyEngine(ABC):
    """策略引擎抽象基类。

    实现负责组合授权/准入/配额/审批规则，返回最终 :class:`PolicyDecision`。
    """

    @abstractmethod
    async def check(self, ctx: Any, action: PolicyAction) -> PolicyDecision:
        """评估一次操作是否被允许。

        Args:
            ctx: 调用上下文（如 actor、correlation_id、审批上下文等）。
            action: 待鉴权操作。

        Returns:
            三态判定 :class:`PolicyDecision`：``allow`` / ``deny`` / ``degraded``。
            **默认拒绝**：无法判定时返回 ``deny``。
        """
        raise NotImplementedError


__all__ = [
    "PolicyAction",
    "PolicyDecision",
    "PolicyEngine",
]
