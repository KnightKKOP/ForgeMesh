"""执行平面实体模型：``Node`` / ``Accelerator`` / ``DeviceInstance`` / ``DeviceProfile`` 等。

对齐 ``docs/architecture/development-plan.md``：

* 5.1 核心资源模型；
* 6.4 ``DeviceProfile`` YAML 示例；
* 7.2 DDL 草案（``node`` / ``accelerator`` / ``device_instance`` 表）；
* ADR-010（能力驱动异构资源模型：厂商差异通过 Provider 显式表达）。

这些实体是控制平面观测到的事实（Observed），不直接作为声明式资源对外暴露；
字段命名与 DDL 保持一致（snake_case），便于持久化层直接映射。
兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

#: 健康状态的通用取值（对齐 DDL 注释）。
#: ``Healthy`` / ``Degraded`` / ``Unhealthy`` / ``Unknown``。
DEFAULT_HEALTH = "Unknown"


class Node(BaseModel):
    """节点（承载加速器与设备实例）。

    Attributes:
        agent_version: 节点侧 Node Agent 版本。
        health: 节点健康状态，默认 ``Unknown``。
        last_seen_at: 最近一次心跳时间；为空表示从未上报。
    """

    model_config = ConfigDict(extra="forbid")

    agent_version: Optional[str] = None
    health: str = DEFAULT_HEALTH
    last_seen_at: Optional[datetime] = None


class Accelerator(BaseModel):
    """物理加速器（如单张 GPU）。

    Attributes:
        vendor: 厂商，如 ``nvidia``。
        model: 型号，如 ``H100``。
        memory_bytes: 显存容量（字节），必须 ``>= 0``。
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str
    model: str
    memory_bytes: int = Field(ge=0)


class DeviceInstance(BaseModel):
    """设备实例（可为 MIG 等分区产生的子设备）。

    Attributes:
        parent_id: 父设备实例 id（MIG 父子关系）；顶层实例为空。
        profile: 关联的 :class:`DeviceProfile` 名称。
        health: 健康状态，默认 ``Unknown``。
    """

    model_config = ConfigDict(extra="forbid")

    parent_id: Optional[str] = None
    profile: Optional[str] = None
    health: str = DEFAULT_HEALTH


class DeviceProfile(BaseModel):
    """设备画像：声明能力（能力驱动模型，ADR-010）。

    声明能力 ≠ 实际具备：实际支持由 Provider 探测并验证。

    Attributes:
        vendor: 厂商标识。
        capabilities: 能力声明（如显存下限、分区、拓扑、算力）。
        runtimes: 支持的运行时标识列表（如 ``["vllm"]``）。
        constraints: 分配约束（如 ``{"allocationMode": "exclusive"}``）。
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    runtimes: List[str] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)


class Topology(BaseModel):
    """设备/节点拓扑（用于亲和性与集合通信感知的调度）。

    Attributes:
        nodes: 参与拓扑的节点/设备标识列表。
        links: 节点间的连接描述（链路带宽/延迟等）。
        discovery: 拓扑发现能力状态。
    """

    model_config = ConfigDict(extra="forbid")

    nodes: List[str] = Field(default_factory=list)
    links: List[Dict[str, Any]] = Field(default_factory=list)
    discovery: str = "unknown"


class HealthReport(BaseModel):
    """健康上报快照。

    Attributes:
        status: 健康状态取值。
        message: 人类可读说明。
        observed_at: 上报观测时间。
    """

    model_config = ConfigDict(extra="forbid")

    status: str = DEFAULT_HEALTH
    message: str = ""
    observed_at: Optional[datetime] = None


class RuntimeInstance(BaseModel):
    """运行中的推理运行时实例。

    Attributes:
        runtime: 运行时标识（如 ``vllm``）。
        version: 运行版本（版本固定，ADR-011）。
        health: 健康状态，默认 ``Unknown``。
        endpoint: 服务端点（若已就绪）。
    """

    model_config = ConfigDict(extra="forbid")

    runtime: str
    version: Optional[str] = None
    health: str = DEFAULT_HEALTH
    endpoint: Optional[str] = None


__all__ = [
    "DEFAULT_HEALTH",
    "Node",
    "Accelerator",
    "DeviceInstance",
    "DeviceProfile",
    "Topology",
    "HealthReport",
    "RuntimeInstance",
]
