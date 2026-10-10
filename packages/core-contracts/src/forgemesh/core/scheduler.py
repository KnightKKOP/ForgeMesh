"""ForgeMesh 调度器契约（资源分配的唯一授权点）。

对齐 ``docs/architecture/development-plan.md`` 5.6 与 ADR-005：

**决策 ≠ 授权（INV-1/INV-3）**：Decision/算法只产出**候选与建议**，绝不直接
分配资源。**只有 :class:`Scheduler` 可执行实际分配**，且分配须在事务中完成
（唯一约束 + 版本检查 + 租约）。可行性判断（:meth:`Scheduler.feasible`）是只读的，
不落分配。

调用方固定顺序：先 Policy（是否允许）后 Scheduler（如何分配）；跳过即视为治理绕过。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ScheduleRequest(BaseModel):
    """调度请求。

    Attributes:
        tenant_id: 多租户边界标识。
        resource_kind: 待分配资源类型（如 ``InferenceService``）。
        requirements: 资源需求（如 accelerator 型号、数量、显存等）。
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    resource_kind: str
    requirements: dict[str, Any] = Field(default_factory=dict)


class FeasibilityResult(BaseModel):
    """可行性判断结果（只读，不产生分配）。

    Attributes:
        feasible: 是否存在满足条件的候选。
        reason: 不可行或补充说明。
        candidates: 可行的候选资源标识列表。
    """

    model_config = ConfigDict(extra="forbid")

    feasible: bool
    reason: str = ""
    candidates: list[str] = Field(default_factory=list)


class AllocationRef(BaseModel):
    """一次资源分配的引用（授权凭证）。

    Attributes:
        allocation_id: 分配唯一标识。
        resource_id: 被分配的物理/逻辑资源标识。
        lease_until: 租约到期时间；``None`` 表示不限时（需由上层策略保证回收）。
    """

    model_config = ConfigDict(extra="forbid")

    allocation_id: str
    resource_id: str
    lease_until: datetime | None = None


class Scheduler(ABC):
    """资源调度器抽象基类。

    **只有 Scheduler 可分配资源**；可行性判断不落分配。
    """

    @abstractmethod
    async def feasible(self, req: ScheduleRequest) -> FeasibilityResult:
        """仅做可行性判断，**不**产生任何分配或副作用。"""
        raise NotImplementedError

    @abstractmethod
    async def allocate(self, req: ScheduleRequest) -> AllocationRef:
        """执行分配（事务 + 唯一约束 + 版本检查 + 租约）。

        这是唯一获授权改变资源占用状态的入口；决策建议不得绕过本方法直接分配。
        """
        raise NotImplementedError

    @abstractmethod
    async def release(self, allocation_id: str) -> None:
        """释放分配与租约（幂等：重复释放同一 allocation_id 不应报错）。"""
        raise NotImplementedError


__all__ = ["ScheduleRequest", "FeasibilityResult", "AllocationRef", "Scheduler"]
