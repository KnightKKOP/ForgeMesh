"""首次适配（first-fit）调度器：按静态加速器清单为资源挑选设备。

实现 :class:`forgemesh.core.scheduler.Scheduler`：

* 需求取自 ``ScheduleRequest.requirements["accelerator"]``（``type`` / ``count`` /
  ``minMemoryGiB``，即 ``spec.resources.accelerator``）；没有加速器需求的请求得到
  不含设备的分配；
* 按清单顺序取第一个能满足全部数量的节点，在该节点内按清单顺序取前 ``count`` 个类型
  精确匹配、显存不低于 ``minMemoryGiB`` 且不属于任何 Active 分配的设备（不跨节点）；
* 分配与设备占用写入注册表（:meth:`SQLiteRegistry.create_allocation`），设备独占由
  数据库约束保证；同一调度器实例内的分配串行执行，其他连接的并发分配抢到同一设备时，
  落败方重新挑选；
* 同一 ``idempotency_key`` 返回同一分配（重启后亦然）；资源（uid）已持有恰好满足
  需求的 Active 分配时直接复用（例如 spec 其他字段变更产生的新 generation），不满足
  时报冲突，须先释放；
* 不设租约（``lease_until`` 为 ``None``）：协调器在删除资源、部署失败（停止实例之后）与
  加速器申请变化时释放分配。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from forgemesh.core.errors import (
    ConflictError,
    ForgeMeshError,
    InvalidStateTransition,
    NotFoundError,
)
from forgemesh.core.scheduler import (
    AllocatedDevice,
    AllocationRef,
    FeasibilityResult,
    Scheduler,
    ScheduleRequest,
)
from forgemesh.resource.allocation import AllocationStatus
from forgemesh.resource.inference import AcceleratorSpec
from pydantic import BaseModel, ConfigDict, ValidationError

from ..registry.store import SQLiteRegistry
from .inventory import Inventory, InventoryDevice

logger = logging.getLogger(__name__)

#: 挑中的设备被并发分配抢走后重新挑选的总尝试次数。
_MAX_ATTEMPTS = 3


class InsufficientCapacityError(ForgeMeshError):
    """清单中没有足够的空闲设备满足需求。"""


class _Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accelerator: AcceleratorSpec | None = None


def _accelerator(req: ScheduleRequest) -> AcceleratorSpec | None:
    try:
        return _Requirements.model_validate(req.requirements).accelerator
    except ValidationError as exc:
        raise ValueError(f"invalid scheduling requirements: {exc}") from exc


def _fits(device: InventoryDevice, accelerator: AcceleratorSpec) -> bool:
    return device.type == accelerator.type and device.memoryGiB >= accelerator.minMemoryGiB


def _to_ref(record: dict[str, Any]) -> AllocationRef:
    return AllocationRef(
        allocation_id=record["id"],
        resource_id=record["resource_id"],
        devices=[AllocatedDevice(**device) for device in record["devices"]],
        lease_until=record["lease_until"],
    )


def _names(devices: list[AllocatedDevice]) -> list[str]:
    return [f"{device.node_id}/{device.device_id}" for device in devices]


class FirstFitScheduler(Scheduler):
    """按静态清单首次适配的调度器（规则见模块说明）。

    Args:
        registry: 权威注册表，分配与设备占用的唯一记录。
        inventory: 启动时加载的加速器清单。
    """

    def __init__(self, registry: SQLiteRegistry, inventory: Inventory) -> None:
        self._registry = registry
        self._inventory = inventory
        self._devices = {
            (node.id, device.id): device for node in inventory.nodes for device in node.devices
        }
        # 首次适配下并发请求总会挑中同一个空闲设备：不串行化时，落败方耗尽重试次数后报冲突，
        # 设备却仍有空闲。
        self._allocate_lock = asyncio.Lock()

    async def feasible(self, req: ScheduleRequest) -> FeasibilityResult:
        """只读判断 ``req`` 能否满足，不写库。

        ``candidates`` 为 :meth:`allocate` 此刻会分配（或已分配给该请求）的设备，
        形如 ``node-a/gpu-0``。

        Raises:
            ValueError: ``req.requirements`` 不合法。
        """
        accelerator = _accelerator(req)
        try:
            existing = await self._existing_allocation(req, accelerator)
            if existing is not None:
                return FeasibilityResult(
                    feasible=True,
                    reason=f"allocation {existing.allocation_id} already serves the request",
                    candidates=_names(existing.devices),
                )
            devices = self._first_fit(accelerator, await self._registry.allocated_devices())
        except (ConflictError, InsufficientCapacityError) as exc:
            return FeasibilityResult(feasible=False, reason=str(exc))
        return FeasibilityResult(feasible=True, candidates=_names(devices))

    async def allocate(self, req: ScheduleRequest) -> AllocationRef:
        """首次适配分配；同一 ``idempotency_key`` 返回同一分配。

        Raises:
            ValueError: ``req.requirements`` 不合法。
            InsufficientCapacityError: 没有足够的空闲设备。
            ConflictError: 幂等键属于其他资源或已释放的分配；资源已持有不满足需求的
                Active 分配；归属资源正在删除；或多次重新挑选后仍被其他连接的并发分配抢先。
            NotFoundError: 归属资源不存在（或 uid 不一致）。
        """
        accelerator = _accelerator(req)
        async with self._allocate_lock:
            attempt = 0
            while True:
                attempt += 1
                existing = await self._existing_allocation(req, accelerator)
                if existing is not None:
                    return existing
                devices = self._first_fit(accelerator, await self._registry.allocated_devices())
                try:
                    record = await self._registry.create_allocation(
                        req.tenant_id, req.resource_ref, req.idempotency_key, devices
                    )
                except ConflictError:
                    if attempt >= _MAX_ATTEMPTS:
                        raise
                    continue
                allocation = _to_ref(record)
                ref = req.resource_ref
                logger.info(
                    "allocation created: allocation_id=%s tenant_id=%s kind=%s namespace=%s "
                    "name=%s uid=%s devices=%s",
                    allocation.allocation_id,
                    req.tenant_id,
                    ref.kind,
                    ref.namespace,
                    ref.name,
                    ref.uid,
                    _names(allocation.devices),
                )
                return allocation

    async def release(self, allocation_id: str) -> None:
        """释放分配及其设备；分配已释放或已过期时直接返回（幂等）。

        Raises:
            NotFoundError: 分配不存在。
        """
        record = await self._registry.get_allocation(allocation_id)
        if record is None:
            raise NotFoundError(f"allocation not found: id={allocation_id}")
        if record["status"] != AllocationStatus.ACTIVE.value:
            return
        try:
            await self._registry.release_allocation(
                allocation_id, record["tenant_id"], reason="released by scheduler"
            )
        except (ConflictError, InvalidStateTransition):
            current = await self._registry.get_allocation(allocation_id)
            if current is not None and current["status"] == AllocationStatus.ACTIVE.value:
                raise
            return
        logger.info(
            "allocation released: allocation_id=%s tenant_id=%s uid=%s",
            allocation_id,
            record["tenant_id"],
            record["resource_id"],
        )

    async def _existing_allocation(
        self, req: ScheduleRequest, accelerator: AcceleratorSpec | None
    ) -> AllocationRef | None:
        """返回已服务于 ``req`` 的 Active 分配：同一幂等键的分配，或资源持有的满足需求的分配。

        Raises:
            ConflictError: 幂等键属于其他资源或已不再 Active 的分配；资源持有的 Active
                分配不满足需求。
        """
        uid = req.resource_ref.uid
        by_key = await self._registry.get_allocation_by_idempotency_key(
            req.idempotency_key, req.tenant_id
        )
        if by_key is not None:
            if by_key["resource_id"] != uid:
                raise ConflictError(
                    f"idempotency_key {req.idempotency_key!r} belongs to another resource"
                )
            if by_key["status"] != AllocationStatus.ACTIVE.value:
                raise ConflictError(
                    f"idempotency_key {req.idempotency_key!r} belongs to allocation "
                    f"{by_key['id']} which is {by_key['status']}",
                )
            return _to_ref(by_key)
        held = await self._registry.get_active_allocation(uid, req.tenant_id)
        if held is None:
            return None
        ref = _to_ref(held)
        if not self._satisfies(ref.devices, accelerator):
            raise ConflictError(
                f"resource uid={uid} holds allocation {ref.allocation_id} "
                f"({_names(ref.devices)}) that does not match the requested accelerator; "
                "it must be released first",
            )
        return ref

    def _first_fit(
        self, accelerator: AcceleratorSpec | None, taken: frozenset[AllocatedDevice]
    ) -> list[AllocatedDevice]:
        """按清单顺序挑选不在 ``taken`` 中的设备。

        Raises:
            InsufficientCapacityError: 没有节点能满足全部数量。
        """
        if accelerator is None:
            return []
        for node in self._inventory.nodes:
            free = []
            for device in node.devices:
                candidate = AllocatedDevice(node_id=node.id, device_id=device.id)
                if _fits(device, accelerator) and candidate not in taken:
                    free.append(candidate)
            if len(free) >= accelerator.count:
                return free[: accelerator.count]
        raise InsufficientCapacityError(
            f"no node has {accelerator.count} free {accelerator.type} device(s) "
            f"with at least {accelerator.minMemoryGiB} GiB"
        )

    def _satisfies(
        self, devices: list[AllocatedDevice], accelerator: AcceleratorSpec | None
    ) -> bool:
        """已分配的设备是否恰好满足需求（数量相等，且每个设备都在清单中并符合要求）。"""
        if accelerator is None:
            return not devices
        if len(devices) != accelerator.count:
            return False
        for device in devices:
            spec = self._devices.get((device.node_id, device.device_id))
            if spec is None or not _fits(spec, accelerator):
                return False
        return True


__all__ = ["FirstFitScheduler", "InsufficientCapacityError"]
