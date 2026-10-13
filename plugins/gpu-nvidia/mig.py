"""NVIDIA MIG (Multi-Instance GPU) 实例管理。

将单块物理 GPU 切分为多个隔离实例（MIG 实例），每实例拥有独立的
显存与计算切片（GPC slice），供多租户共享同一 GPU 而互不干扰。

架构不变量：

* **状态机严格**：实例状态仅在合法转移路径上变化，非法转移抛
  :class:`MigStateError`，调用方据此做显式决策。
* **并发安全**：同一 GPU 上实例创建 / 销毁互斥（per-GPU ``asyncio.Lock``），
  避免资源计数竞态导致超分。
* **资源不超分**：创建前校验显存与计算切片余量，不足则抛
  :class:`MigResourceError`。
* **测试友好**：GPU 能力提供者、profile 发现与健康检查函数均可注入 mock。

状态机::

    NotCreated ──create──▶ Created ──health──▶ Healthy / Degraded
                                   └──destroy──▶ Destroyed
                     Healthy / Degraded ──destroy──▶ Destroyed
                     Healthy ⇄ Degraded（健康检查可反复执行）

基于 Pydantic v2。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# 状态机取值
# --------------------------------------------------------------------------- #

NOT_CREATED = "NotCreated"
CREATED = "Created"
HEALTHY = "Healthy"
DEGRADED = "Degraded"
DESTROYED = "Destroyed"

#: 合法状态转移表：当前状态 → 允许的目标状态集合。
_TRANSITIONS: dict[str, frozenset[str]] = {
    NOT_CREATED: frozenset({CREATED}),
    CREATED: frozenset({HEALTHY, DEGRADED, DESTROYED}),
    HEALTHY: frozenset({DEGRADED, DESTROYED}),
    DEGRADED: frozenset({HEALTHY, DESTROYED}),
    DESTROYED: frozenset(),
}

#: 健康检查可输出的合法状态。
_HEALTH_STATES = frozenset({HEALTHY, DEGRADED})


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class MigStateError(Exception):
    """MIG 状态机非法转移。"""


class MigResourceError(Exception):
    """MIG 资源不足（显存或计算切片超限）。"""


class MigNotFoundError(Exception):
    """MIG 实例不存在。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


class MigProfile(BaseModel):
    """MIG profile 定义（计算切片数 + 显存 + 算力占比）。

    Attributes:
        name: profile 名称（如 ``1g.5gb`` / ``2g.10gb`` / ``7g.40gb``）。
        slice_count: 计算切片数（GPC slice），必须 ``>= 1``。
        memory_gb: 该 profile 分配的显存（GB），必须 ``>= 1``。
        compute_percentage: 算力占比（0–100）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    slice_count: int = Field(ge=1)
    memory_gb: int = Field(ge=1)
    compute_percentage: int = Field(ge=0, le=100)


#: 预定义 MIG profile（对齐 NVIDIA H100 80GB MIG 分区模式）。
MIG_PROFILES: dict[str, MigProfile] = {
    "1g.5gb": MigProfile(name="1g.5gb", slice_count=1, memory_gb=5, compute_percentage=14),
    "1g.10gb": MigProfile(name="1g.10gb", slice_count=1, memory_gb=10, compute_percentage=14),
    "2g.10gb": MigProfile(name="2g.10gb", slice_count=2, memory_gb=10, compute_percentage=28),
    "3g.20gb": MigProfile(name="3g.20gb", slice_count=3, memory_gb=20, compute_percentage=43),
    "4g.20gb": MigProfile(name="4g.20gb", slice_count=4, memory_gb=20, compute_percentage=57),
    "7g.40gb": MigProfile(name="7g.40gb", slice_count=7, memory_gb=40, compute_percentage=100),
}


class MigInstance(BaseModel):
    """MIG 实例（一块 GPU 上的一个隔离分区）。

    Attributes:
        instance_id: 实例唯一标识（UUID 前缀）。
        gpu_id: 所属物理 GPU 索引。
        profile: 该实例的 MIG profile。
        placement: 计算切片放置标识（如 ``gpu0:slice0-2``）。
        tenant_id: 租户标识。
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    gpu_id: int
    profile: MigProfile
    placement: str
    tenant_id: str


class MigHealthStatus(BaseModel):
    """MIG 实例健康状态快照。

    Attributes:
        instance_id: 实例标识。
        status: 健康状态（``Healthy`` / ``Degraded``）。
        gpu_id: 所属 GPU 索引。
        timestamp: 采集时间戳（Unix epoch 秒）。
        details: 诊断详情（原始指标等）。
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    status: str
    gpu_id: int
    timestamp: float = Field(default_factory=time.time)
    details: dict[str, Any] = Field(default_factory=dict)


class MigCapabilityReport(BaseModel):
    """MIG 能力报告（单 GPU 的显存 / 切片总量、已用、可用）。

    Attributes:
        gpu_id: GPU 索引。
        total_memory_gb: 总显存（GB）。
        total_compute_slices: 总计算切片数。
        used_memory_gb: 已分配显存（GB）。
        used_compute_slices: 已分配计算切片数。
        available_memory_gb: 可用显存（GB）。
        available_compute_slices: 可用计算切片数。
        instances: 该 GPU 上当前活跃的 MIG 实例列表。
    """

    model_config = ConfigDict(extra="forbid")

    gpu_id: int
    total_memory_gb: int
    total_compute_slices: int
    used_memory_gb: int
    used_compute_slices: int
    available_memory_gb: int
    available_compute_slices: int
    instances: list[MigInstance] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 可注入函数类型与默认实现
# --------------------------------------------------------------------------- #

#: GPU 能力提供者：返回 ``{"total_memory_gb": int, "total_compute_slices": int}``。
GpuCapabilityProvider = Callable[[int], Awaitable[dict[str, Any]]]

#: MIG profile 发现函数：返回可用 profile 名称列表。
MigProfileDiscoveryFn = Callable[[int], Awaitable[list[str]]]

#: MIG 实例健康检查函数：返回原始指标 dict（含 ``status`` 键）。
MigHealthCheckFn = Callable[[str], Awaitable[dict[str, Any]]]


async def _default_gpu_capability(gpu_id: int) -> dict[str, Any]:
    """默认 GPU 能力：H100 80GB（80GB 显存 / 7 计算切片）。"""
    return {"total_memory_gb": 80, "total_compute_slices": 7}


async def _default_discover_profiles(gpu_id: int) -> list[str]:
    """默认 profile 发现：返回全部预定义 profile 名称。"""
    return list(MIG_PROFILES)


async def _default_health_check(instance_id: str) -> dict[str, Any]:
    """默认健康检查：无真实硬件时返回健康指标。"""
    return {"status": HEALTHY, "utilization": 0.0, "memory_used": 0}


# --------------------------------------------------------------------------- #
# MigManager
# --------------------------------------------------------------------------- #


class MigManager:
    """MIG 实例管理器：创建 / 销毁 / 列举 / 发现 / 健康检查 / 能力报告。

    Args:
        gpu_capability_provider: GPU 能力提供者（可注入 mock）。
        profile_discovery_fn: profile 发现函数（可注入 mock）。
        health_check_fn: 实例健康检查函数（可注入 mock）。
    """

    def __init__(
        self,
        *,
        gpu_capability_provider: GpuCapabilityProvider | None = None,
        profile_discovery_fn: MigProfileDiscoveryFn | None = None,
        health_check_fn: MigHealthCheckFn | None = None,
    ) -> None:
        self._get_gpu_capability: GpuCapabilityProvider = (
            gpu_capability_provider or _default_gpu_capability
        )
        self._discover_profiles_fn: MigProfileDiscoveryFn = (
            profile_discovery_fn or _default_discover_profiles
        )
        self._health_check_fn: MigHealthCheckFn = health_check_fn or _default_health_check
        # 实例注册表：instance_id -> MigInstance
        self._instances: dict[str, MigInstance] = {}
        # 状态注册表：instance_id -> 状态字符串
        self._states: dict[str, str] = {}
        # 每 GPU 一把异步锁，保证同 GPU 创建 / 销毁互斥
        self._gpu_locks: dict[int, asyncio.Lock] = {}

    def _gpu_lock(self, gpu_id: int) -> asyncio.Lock:
        """获取（或惰性创建）指定 GPU 的互斥锁。"""
        if gpu_id not in self._gpu_locks:
            self._gpu_locks[gpu_id] = asyncio.Lock()
        return self._gpu_locks[gpu_id]

    def _transition(self, instance_id: str, target: str) -> None:
        """执行状态转移；非法转移抛 :class:`MigStateError`。"""
        current = self._states.get(instance_id, NOT_CREATED)
        allowed = _TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            msg = f"非法状态转移: 实例 {instance_id} {current} -> {target}"
            raise MigStateError(msg)
        self._states[instance_id] = target

    def _is_active(self, instance_id: str) -> bool:
        """实例是否活跃（已创建且未销毁）。"""
        state = self._states.get(instance_id, NOT_CREATED)
        return state != NOT_CREATED and state != DESTROYED

    def _used_resources(self, gpu_id: int) -> tuple[int, int]:
        """累计指定 GPU 上活跃实例的 (已用显存 GB, 已用切片数)。"""
        used_mem = 0
        used_slices = 0
        for inst_id, inst in self._instances.items():
            if inst.gpu_id == gpu_id and self._is_active(inst_id):
                used_mem += inst.profile.memory_gb
                used_slices += inst.profile.slice_count
        return used_mem, used_slices

    async def create_instance(
        self,
        gpu_id: int,
        profile: MigProfile,
        tenant_id: str,
    ) -> MigInstance:
        """在指定 GPU 上创建 MIG 实例。

        同一 GPU 上的创建操作互斥；创建前校验显存与计算切片余量，
        不足时抛 :class:`MigResourceError`。

        Returns:
            新创建的 :class:`MigInstance`（状态为 ``Created``）。
        """
        async with self._gpu_lock(gpu_id):
            caps = await self._get_gpu_capability(gpu_id)
            total_mem = int(caps.get("total_memory_gb", 0))
            total_slices = int(caps.get("total_compute_slices", 0))

            used_mem, used_slices = self._used_resources(gpu_id)

            if used_mem + profile.memory_gb > total_mem:
                msg = (
                    f"GPU {gpu_id} 显存不足: 已用 {used_mem}GB + 需要 "
                    f"{profile.memory_gb}GB > 总量 {total_mem}GB"
                )
                raise MigResourceError(msg)
            if used_slices + profile.slice_count > total_slices:
                msg = (
                    f"GPU {gpu_id} 计算切片不足: 已用 {used_slices} + 需要 "
                    f"{profile.slice_count} > 总量 {total_slices}"
                )
                raise MigResourceError(msg)

            instance_id = f"mig-{uuid.uuid4().hex[:12]}"
            start_slice = used_slices
            end_slice = used_slices + profile.slice_count - 1
            placement = f"gpu{gpu_id}:slice{start_slice}-{end_slice}"
            instance = MigInstance(
                instance_id=instance_id,
                gpu_id=gpu_id,
                profile=profile,
                placement=placement,
                tenant_id=tenant_id,
            )
            # 状态转移 NotCreated -> Created
            self._transition(instance_id, CREATED)
            self._instances[instance_id] = instance
            return instance

    async def destroy_instance(self, instance_id: str) -> None:
        """销毁 MIG 实例。

        实例不存在抛 :class:`MigNotFoundError`；已销毁等非法状态抛
        :class:`MigStateError`。
        """
        if instance_id not in self._instances:
            msg = f"MIG 实例不存在: {instance_id}"
            raise MigNotFoundError(msg)

        gpu_id = self._instances[instance_id].gpu_id
        async with self._gpu_lock(gpu_id):
            self._transition(instance_id, DESTROYED)

    async def list_instances(self, gpu_id: int | None = None) -> list[MigInstance]:
        """列出 MIG 实例，仅返回活跃实例（已销毁的不含）。

        Args:
            gpu_id: 若给定则仅返回该 GPU 上的实例；``None`` 返回全部。
        """
        result: list[MigInstance] = []
        for inst_id, inst in self._instances.items():
            if not self._is_active(inst_id):
                continue
            if gpu_id is not None and inst.gpu_id != gpu_id:
                continue
            result.append(inst)
        return result

    async def discover_profiles(self, gpu_id: int) -> list[MigProfile]:
        """发现指定 GPU 上可用的 MIG profile。

        通过可注入的发现函数获取 profile 名称列表，映射为
        :class:`MigProfile`；未知名称被跳过（不抛异常，便于前向兼容）。
        """
        names = await self._discover_profiles_fn(gpu_id)
        profiles: list[MigProfile] = []
        for name in names:
            profile = MIG_PROFILES.get(name)
            if profile is not None:
                profiles.append(profile)
        return profiles

    async def health_check(self, instance_id: str) -> MigHealthStatus:
        """MIG 实例健康检查。

        实例不存在抛 :class:`MigNotFoundError`；未创建或已销毁的实例
        抛 :class:`MigStateError`。检查函数抛异常时状态降级为 ``Degraded``。
        """
        if instance_id not in self._instances:
            msg = f"MIG 实例不存在: {instance_id}"
            raise MigNotFoundError(msg)

        current = self._states.get(instance_id, NOT_CREATED)
        if current in (NOT_CREATED, DESTROYED):
            msg = f"非法状态转移: 实例 {instance_id} {current} -> 健康检查"
            raise MigStateError(msg)

        inst = self._instances[instance_id]
        try:
            raw = await self._health_check_fn(instance_id)
        except Exception as exc:
            self._transition(instance_id, DEGRADED)
            return MigHealthStatus(
                instance_id=instance_id,
                status=DEGRADED,
                gpu_id=inst.gpu_id,
                details={"error": str(exc)},
            )

        raw_status = str(raw.get("status", HEALTHY))
        if raw_status not in _HEALTH_STATES:
            raw_status = DEGRADED
        self._transition(instance_id, raw_status)
        return MigHealthStatus(
            instance_id=instance_id,
            status=raw_status,
            gpu_id=inst.gpu_id,
            details=dict(raw),
        )

    async def get_capability_report(self, gpu_id: int) -> MigCapabilityReport:
        """生成指定 GPU 的 MIG 能力报告（显存 / 切片总量、已用、可用）。"""
        caps = await self._get_gpu_capability(gpu_id)
        total_mem = int(caps.get("total_memory_gb", 0))
        total_slices = int(caps.get("total_compute_slices", 0))

        used_mem, used_slices = self._used_resources(gpu_id)
        active: list[MigInstance] = []
        for inst_id, inst in self._instances.items():
            if inst.gpu_id == gpu_id and self._is_active(inst_id):
                active.append(inst)

        return MigCapabilityReport(
            gpu_id=gpu_id,
            total_memory_gb=total_mem,
            total_compute_slices=total_slices,
            used_memory_gb=used_mem,
            used_compute_slices=used_slices,
            available_memory_gb=total_mem - used_mem,
            available_compute_slices=total_slices - used_slices,
            instances=active,
        )


__all__ = [
    "MigProfile",
    "MigInstance",
    "MigHealthStatus",
    "MigCapabilityReport",
    "MigManager",
    "MigStateError",
    "MigResourceError",
    "MigNotFoundError",
    "MIG_PROFILES",
    "GpuCapabilityProvider",
    "MigProfileDiscoveryFn",
    "MigHealthCheckFn",
    "NOT_CREATED",
    "CREATED",
    "HEALTHY",
    "DEGRADED",
    "DESTROYED",
]
