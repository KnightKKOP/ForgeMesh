"""资源分配与租约模型（并发安全核心）。

对齐 ``docs/architecture/development-plan.md``：

* 5.1 核心资源模型；
* 7.2 DDL 草案（``allocation`` / ``resource_lease`` 表）与「并发分配正确性」；
* R-04 并发分配风险缓解：唯一约束 + 版本检查 + 租约 + 幂等键。

关键语义：

* ``AllocationStatus`` 只有 ``Active → {Released, Expired}`` 的单向转移；
* 同一设备同一时刻**至多一个** ``Active`` 分配（由部分唯一索引兜底，此处以状态表达）；
* ``idempotency_key`` 保证重复分配请求幂等（不产生重复副作用）。

字段命名与 DDL 保持一致（snake_case），便于持久化层直接映射。
兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AllocationStatus(StrEnum):
    """分配状态。

    * ``ACTIVE``    —— 生效中（持有资源）。
    * ``RELEASED``  —— 已释放（主动归还）。
    * ``EXPIRED``   —— 已过期（租约到期，被动回收）。
    """

    ACTIVE = "Active"
    RELEASED = "Released"
    EXPIRED = "Expired"


class Allocation(BaseModel):
    """资源分配记录。

    Attributes:
        resource_id: 被分配的加速器 / 设备实例 id。
        tenant_id: 归属租户（多租户隔离边界）。
        owner_uid: 归属资源 uid（如 ``InferenceService`` 的 uid）；平台级分配可为空。
        idempotency_key: 幂等键，唯一（重复请求不产生重复分配）。
        status: 分配状态，默认 ``Active``。
        lease_until: 租约到期时间；为空表示无期限（不推荐）。
    """

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    tenant_id: str
    owner_uid: str | None = None
    idempotency_key: str
    status: AllocationStatus = AllocationStatus.ACTIVE
    lease_until: datetime | None = None


class ResourceLease(BaseModel):
    """资源租约（由协调控制器实例续租）。

    Attributes:
        allocation_id: 关联的 :class:`Allocation` id。
        lease_until: 本次租约到期时间。
        renewable: 是否允许续租。
    """

    model_config = ConfigDict(extra="forbid")

    allocation_id: str
    lease_until: datetime
    renewable: bool = True


__all__ = [
    "AllocationStatus",
    "Allocation",
    "ResourceLease",
]
