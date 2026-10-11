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

#: 策略判定三态（对齐 development-plan 5.6 / architecture 7.2、14.2）。
#:
#: * ``allow`` —— 允许（可附带 obligations 约束，如「必须记录审计」）；
#: * ``deny`` —— 拒绝（**默认拒绝**：无法判定时返回 deny）；
#: * ``require_approval`` —— 高风险等场景要求人工审批，**不是放行**，
#:   审批通过前不得执行（architecture 7.5：高风险操作不允许通过降级绕过审批）。
#:
#: 注：历史实现中的 ``degraded``（降级放行）与架构文档 §7.5 冲突，已移除；
#: 降级/条件类信息改由 ``reason`` 与 ``obligations`` 表达，不作为决策类型。
PolicyDecisionType = Literal["allow", "deny", "require_approval"]


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
    """策略判定结果（三态：allow / deny / require_approval）。

    Attributes:
        decision: 判定结论——``allow``（允许，可带约束）、``deny``（拒绝）、
            ``require_approval``（要求人工审批，审批通过前不放行）。
        reason: 判定理由（供审计/解释）。
        obligations: 附加约束（如「必须记录审计」「require_mfa」）；
            条件授权、配额接近上限等降级类信息在此表达，**不改变** allow 语义。
        policy_id: 命中的策略规则标识（用于审计追溯，development-plan 5.6）。
        approvers: 要求的审批人列表（``require_approval`` 时有效）。
    """

    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecisionType
    reason: str = ""
    obligations: list[str] = Field(default_factory=list)
    policy_id: str | None = None
    approvers: list[str] | None = None


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
            三态判定 :class:`PolicyDecision`：``allow`` / ``deny`` / ``require_approval``。
            **默认拒绝**：无法判定时返回 ``deny``；高风险操作返回
            ``require_approval``，审批通过前不放行。
        """
        raise NotImplementedError


__all__ = [
    "PolicyAction",
    "PolicyDecision",
    "PolicyDecisionType",
    "PolicyEngine",
]
