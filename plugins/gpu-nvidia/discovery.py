"""NVIDIA GPU 发现与能力探测（对齐 development-plan 5.1 / ADR-010）。

枚举 GPU 设备并采集能力（显存、算力、MIG 支持等），与
:class:`forgemesh.resource.node.DeviceProfile` 做能力匹配。

架构不变量：

* **声明能力 ≠ 实际具备**（ADR-010）：``DeviceProfile`` 声明的能力需由本模块
  探测结果验证后才视为实际支持。
* **测试友好**：枚举函数可注入，无真实 GPU 时用 mock 即可测试。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from forgemesh.resource.node import DeviceProfile
from pydantic import BaseModel, ConfigDict, Field

#: 设备枚举函数类型：返回原始设备信息列表（每项为 dict，字段对齐 ``GPUDevice``）。
DeviceEnumerator = Callable[[], Awaitable[list[dict[str, Any]]]]


class GPUDevice(BaseModel):
    """GPU 设备信息。

    Attributes:
        index: 设备索引（0-based）。
        name: 设备名称（如 ``NVIDIA H100 80GB HBM3``）。
        uuid: 设备 UUID（如 ``GPU-xxxxxxxx-...``）。
        memory_bytes: 显存容量（字节），必须 ``>= 0``。
        driver_version: 驱动版本（如 ``535.104.05``）。
        cuda_version: CUDA 运行时版本（如 ``12.2``）。
        compute_capability: 算力版本 ``(major, minor)``，如 ``(8, 9)``。
        mig_enabled: 是否启用 MIG（Multi-Instance GPU）分区。
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    name: str
    uuid: str
    memory_bytes: int = Field(ge=0)
    driver_version: str = ""
    cuda_version: str = ""
    compute_capability: tuple[int, int] = (0, 0)
    mig_enabled: bool = False


class DiscoveryResult(BaseModel):
    """GPU 发现结果。

    Attributes:
        devices: 发现的 GPU 设备列表。
        topology: 拓扑信息（设备数、索引、链路等）。
        timestamp: 发现时间戳（Unix epoch 秒）。
    """

    model_config = ConfigDict(extra="forbid")

    devices: list[GPUDevice] = Field(default_factory=list)
    topology: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)


def _decode(value: bytes | str) -> str:
    """将 pynvml 返回的 bytes/str 统一为 str。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def _default_enumerate() -> list[dict[str, Any]]:
    """默认 GPU 枚举：通过 pynvml 采集设备原始信息。

    环境无 pynvml 或 NVML 初始化失败时返回空列表（由调用方注入 mock 以测试）。
    """
    try:
        import pynvml as nvml  # noqa: PLC0415
        nvml.nvmlInit()
        count = nvml.nvmlDeviceGetCount()
        devices: list[dict[str, Any]] = []
        for index in range(count):
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            mem = nvml.nvmlDeviceGetMemoryInfo(handle)
            devices.append({
                "index": index,
                "name": _decode(nvml.nvmlDeviceGetName(handle)),
                "uuid": _decode(nvml.nvmlDeviceGetUUID(handle)),
                "memory_bytes": int(mem.total),
            })
        nvml.nvmlShutdown()
    except Exception:
        return []
    else:
        return devices


class GPUDiscoverer:
    """GPU 发现器：枚举设备、采集能力、匹配 DeviceProfile。

    Args:
        enumerator: 设备枚举函数（可注入 mock）；默认尝试 pynvml。
    """

    def __init__(self, enumerator: DeviceEnumerator | None = None) -> None:
        self._enumerate: DeviceEnumerator = enumerator or _default_enumerate

    async def discover(self) -> DiscoveryResult:
        """枚举 GPU 设备并采集能力，返回 :class:`DiscoveryResult`。"""
        raw_devices = await self._enumerate()
        devices = [self._build_device(item) for item in raw_devices]
        topology = self._build_topology(devices)
        return DiscoveryResult(devices=devices, topology=topology)

    async def match_profile(self, device: GPUDevice, profile: DeviceProfile) -> bool:
        """检查设备是否匹配 :class:`DeviceProfile` 声明的能力。

        匹配规则（全部满足才返回 True）：

        1. ``profile.vendor`` 为 ``nvidia``（大小写不敏感）。
        2. ``capabilities["min_memory_bytes"]``（若存在）≤ ``device.memory_bytes``。
        3. ``capabilities["min_compute_capability"]``（若存在）≤ ``device.compute_capability``。
        4. ``capabilities["mig_required"]``（若为 True）要求 ``device.mig_enabled``。
        5. ``capabilities["model"]``（若存在）为 ``device.name`` 子串。
        """
        if profile.vendor.lower() != "nvidia":
            return False

        caps = profile.capabilities

        min_mem = caps.get("min_memory_bytes")
        if min_mem is not None and device.memory_bytes < int(min_mem):
            return False

        min_cc = caps.get("min_compute_capability")
        if min_cc is not None and list(device.compute_capability) < list(min_cc):
            return False

        if caps.get("mig_required") and not device.mig_enabled:
            return False

        required_model = caps.get("model")
        if required_model is not None:
            return str(required_model) in device.name
        return True

    async def collect_capabilities(self, device: GPUDevice) -> dict[str, Any]:
        """采集设备能力（显存、算力、MIG 支持等），返回能力字典。"""
        major, minor = device.compute_capability
        return {
            "name": device.name,
            "uuid": device.uuid,
            "memory_bytes": device.memory_bytes,
            "memory_gib": round(device.memory_bytes / (1024**3), 3),
            "compute_capability": f"{major}.{minor}",
            "compute_capability_tuple": [major, minor],
            "mig_enabled": device.mig_enabled,
            "driver_version": device.driver_version,
            "cuda_version": device.cuda_version,
        }

    @staticmethod
    def _build_device(raw: dict[str, Any]) -> GPUDevice:
        """从原始 dict 构建 :class:`GPUDevice`。"""
        cc = raw.get("compute_capability", (0, 0))
        return GPUDevice(
            index=int(raw["index"]),
            name=str(raw.get("name", "unknown")),
            uuid=str(raw.get("uuid", "")),
            memory_bytes=int(raw.get("memory_bytes", 0)),
            driver_version=str(raw.get("driver_version", "")),
            cuda_version=str(raw.get("cuda_version", "")),
            compute_capability=tuple(cc),  # type: ignore[arg-type]
            mig_enabled=bool(raw.get("mig_enabled", False)),
        )

    @staticmethod
    def _build_topology(devices: list[GPUDevice]) -> dict[str, Any]:
        """构建简单拓扑信息。"""
        return {
            "device_count": len(devices),
            "device_indices": [d.index for d in devices],
            "links": [],
        }


__all__ = [
    "GPUDevice",
    "DiscoveryResult",
    "GPUDiscoverer",
    "DeviceEnumerator",
]
