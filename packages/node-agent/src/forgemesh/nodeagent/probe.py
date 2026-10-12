"""设备/驱动探测与资源快照上报（WP-3.1）。

:class:`DeviceProber` 探测本地加速器与设备实例，生成 :class:`ProbeResult`；
支持注入 ``probe_func`` 以在测试中替换真实探测（如 ``nvidia-smi`` 解析）。

``snapshot()`` 在探测基础上组装 :class:`~forgemesh.resource.node.Node` +
Accelerators + DeviceInstances 的资源快照（JSON 兼容 dict），供
:class:`SnapshotReporter` 上报控制平面。

对齐 architecture 5.1（核心资源模型）与 ADR-010（能力驱动异构资源模型）。
基于 Pydantic v2。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from forgemesh.resource.node import (
    Accelerator,
    DeviceInstance,
    Node,
    Topology,
)
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from forgemesh.nodeagent.server import AgentServer


class ProbeResult(BaseModel):
    """探测结果。

    Attributes:
        accelerators: 探测到的物理加速器列表。
        device_instances: 设备实例列表（含 MIG 等分区子设备）。
        topology: 设备/节点拓扑。
    """

    model_config = ConfigDict(extra="forbid")

    accelerators: list[Accelerator] = Field(default_factory=list)
    device_instances: list[DeviceInstance] = Field(default_factory=list)
    topology: Topology = Field(default_factory=Topology)


#: 探测函数签名：返回 :class:`ProbeResult` 的协程。
ProbeFunc = Callable[[], Awaitable[ProbeResult]]


class DeviceProber:
    """设备探测器。

    Args:
        probe_func: 注入的探测协程；``None`` 时使用默认空探测（不执行真实设备操作）。
        agent_version: 节点 Agent 版本，写入快照 :class:`Node`。
    """

    def __init__(
        self,
        *,
        probe_func: ProbeFunc | None = None,
        agent_version: str | None = None,
    ) -> None:
        self._probe_func = probe_func
        self._agent_version = agent_version

    async def probe(self) -> ProbeResult:
        """探测本地设备，返回 :class:`ProbeResult`。"""
        if self._probe_func is not None:
            return await self._probe_func()
        return await self._default_probe()

    async def _default_probe(self) -> ProbeResult:
        """默认探测：不执行真实设备操作，返回空结果。"""
        return ProbeResult()

    async def snapshot(self) -> dict[str, Any]:
        """生成资源快照（Node + Accelerators + DeviceInstances + Topology）。

        返回值为 JSON 兼容 dict（datetime 转 ISO 字符串），可直接上报控制平面。
        """
        result = await self.probe()
        now = datetime.now(UTC)
        node = Node(agent_version=self._agent_version, last_seen_at=now)
        return {
            "node": node.model_dump(mode="json"),
            "accelerators": [a.model_dump(mode="json") for a in result.accelerators],
            "device_instances": [
                d.model_dump(mode="json") for d in result.device_instances
            ],
            "topology": result.topology.model_dump(mode="json"),
        }


class SnapshotReporter:
    """快照上报。

    Args:
        agent_id: 节点 Agent 标识，用于控制平面鉴权快照归属。
    """

    def __init__(self, *, agent_id: str) -> None:
        self.agent_id = agent_id

    async def report(self, server: AgentServer, snapshot: dict[str, Any]) -> bool:
        """上报快照到控制平面；未注册 agent 返回 ``False``。"""
        return await server.receive_snapshot(
            agent_id=self.agent_id,
            snapshot=snapshot,
        )


__all__ = [
    "ProbeResult",
    "DeviceProber",
    "SnapshotReporter",
]
