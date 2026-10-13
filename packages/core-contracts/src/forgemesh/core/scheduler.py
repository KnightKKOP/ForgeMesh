"""ForgeMesh 调度器契约（资源分配的唯一授权点）。

对齐 ``docs/architecture/development-plan.md`` 5.6 与 ADR-005：

**决策 ≠ 授权（INV-1/INV-3）**：Decision/算法只产出**候选与建议**，绝不直接
分配资源。**只有 :class:`Scheduler` 可执行实际分配**，且分配须在事务中完成
（唯一约束 + 版本检查 + 租约）。可行性判断（:meth:`Scheduler.feasible`）是只读的，
不落分配。

**归属与幂等**：:class:`ScheduleRequest` 携带被服务资源的身份（:class:`ResourceRef`，
以 ``uid`` 为准）与幂等键。同一租户内同一资源 uid 至多持有一个生效分配；以同一
幂等键重复调用 :meth:`Scheduler.allocate` 得到同一分配，不会产生第二个分配。

调用方固定顺序：先 Policy（是否允许）后 Scheduler（如何分配）；跳过即视为治理绕过。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResourceRef(BaseModel):
    """被服务资源的身份（分配的归属方）。

    Attributes:
        kind: 资源类型（如 ``InferenceService``）。
        namespace: 命名空间；``None`` 表示不带命名空间的资源。
        name: 资源名。
        uid: 资源稳定标识（INV-4）。分配按 uid 归属：资源删除后以同名重建得到新的
            uid，不会继承旧资源的分配。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    namespace: str | None = None
    name: str
    uid: str = Field(min_length=1)


class ScheduleRequest(BaseModel):
    """调度请求。

    Attributes:
        tenant_id: 多租户边界标识。
        resource_ref: 被服务资源的身份；分配按 ``(tenant_id, resource_ref.uid)`` 归属。
        idempotency_key: 幂等键：以同一键重复调用 :meth:`Scheduler.allocate` 返回同一
            分配（协调器由资源 uid 与 generation 派生）。
        requirements: 资源需求，如 ``{"accelerator": {"type": "nvidia-gpu", "count": 1,
            "minMemoryGiB": 16}}``；为空表示不需要加速器。
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    resource_ref: ResourceRef
    idempotency_key: str = Field(min_length=1)
    requirements: dict[str, Any] = Field(default_factory=dict)

    @property
    def resource_kind(self) -> str:
        """被服务资源的类型，等同 ``resource_ref.kind``。"""
        return self.resource_ref.kind


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


class AllocatedDevice(BaseModel):
    """分配占用的一个加速器设备。

    Attributes:
        node_id: 设备所在节点的标识。
        device_id: 设备标识（在节点内唯一）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    device_id: str


class AllocationRef(BaseModel):
    """一次资源分配的引用（授权凭证）。

    Attributes:
        allocation_id: 分配唯一标识。
        resource_id: 分配归属资源的 uid（即 ``ScheduleRequest.resource_ref.uid``）。
        devices: 分配占用的加速器设备；不需要加速器的分配为空。
        lease_until: 租约到期时间；``None`` 表示不限时（需由上层策略保证回收）。
    """

    model_config = ConfigDict(extra="forbid")

    allocation_id: str
    resource_id: str
    devices: list[AllocatedDevice] = Field(default_factory=list)
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
        以同一 ``req.idempotency_key`` 重复调用（重放、进程重启后重试）返回同一分配；
        同一租户内同一 ``req.resource_ref.uid`` 至多持有一个生效分配。
        """
        raise NotImplementedError

    @abstractmethod
    async def release(self, allocation_id: str) -> None:
        """释放分配与租约（幂等：重复释放同一 allocation_id 不应报错）。"""
        raise NotImplementedError


__all__ = [
    "ResourceRef",
    "ScheduleRequest",
    "FeasibilityResult",
    "AllocatedDevice",
    "AllocationRef",
    "Scheduler",
]
