"""NVIDIA GPU 周期性设备健康检查。

按阈值评估设备健康状态并降级上报：

* 温度超阈值 → ``Degraded``
* ECC 错误超阈值 → ``Unhealthy``
* 否则 → ``Healthy``

架构不变量：

* **观测 ≠ 授权**：健康状态仅描述事实，不直接改变调度决策。
* **测试友好**：检查函数可注入 mock，监控循环支持 ``max_iterations`` 限制。

基于 Pydantic v2。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: 单设备检查函数类型：返回原始指标 dict。
DeviceCheckFn = Callable[[int], Awaitable[dict[str, Any]]]

#: 监控回调类型：接收本轮所有设备健康状态列表。
MonitorCallback = Callable[[list["GPUHealthStatus"]], Awaitable[None]]

#: 健康状态取值。
HEALTHY = "Healthy"
DEGRADED = "Degraded"
UNHEALTHY = "Unhealthy"


class GPUHealthStatus(BaseModel):
    """GPU 健康状态快照。

    Attributes:
        device_index: 设备索引。
        status: 健康状态（``Healthy`` / ``Degraded`` / ``Unhealthy``）。
        temperature: 温度（摄氏度）。
        utilization: GPU 利用率（0–100）。
        memory_used: 已用显存（字节）。
        memory_total: 总显存（字节）。
        ecc_errors: ECC 错误计数。
        timestamp: 采集时间戳（Unix epoch 秒）。
    """

    model_config = ConfigDict(extra="forbid")

    device_index: int
    status: str = UNHEALTHY
    temperature: float = 0.0
    utilization: float = 0.0
    memory_used: int = 0
    memory_total: int = 0
    ecc_errors: int = 0
    timestamp: float = Field(default_factory=time.time)


async def _default_check(device_index: int) -> dict[str, Any]:
    """默认设备检查：通过 pynvml 采集指标，不可用返回空 dict。"""
    try:
        import pynvml as nvml  # noqa: PLC0415
        nvml.nvmlInit()
        handle = nvml.nvmlDeviceGetHandleByIndex(device_index)
        temp = int(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
        util = nvml.nvmlDeviceGetUtilizationRates(handle)
        mem = nvml.nvmlDeviceGetMemoryInfo(handle)
        ecc = int(nvml.nvmlDeviceGetTotalEccErrors(
            handle, nvml.NVML_SINGLE_BIT_ECC_ERROR,
        ))
        nvml.nvmlShutdown()
    except Exception:
        return {}
    else:
        return {
            "temperature": temp,
            "utilization": util.gpu,
            "memory_used": int(mem.used),
            "memory_total": int(mem.total),
            "ecc_errors": ecc,
        }


class GPUHealthChecker:
    """GPU 健康检查器。

    Args:
        device_count: 设备数量（用于 ``check_all`` / ``monitor_loop``）。
        check_fn: 单设备检查函数（可注入 mock）。
        temp_threshold: 温度降级阈值（摄氏度），超过则 ``Degraded``。
        ecc_threshold: ECC 错误降级阈值，超过则 ``Unhealthy``。
    """

    def __init__(
        self,
        *,
        device_count: int = 0,
        check_fn: DeviceCheckFn | None = None,
        temp_threshold: float = 85.0,
        ecc_threshold: int = 10,
    ) -> None:
        self._device_count = device_count
        self._check_fn: DeviceCheckFn = check_fn or _default_check
        self._temp_threshold = temp_threshold
        self._ecc_threshold = ecc_threshold

    async def check_device(self, device_index: int) -> GPUHealthStatus:
        """检查单个 GPU 健康，返回 :class:`GPUHealthStatus`。"""
        try:
            raw = await self._check_fn(device_index)
        except Exception:
            return GPUHealthStatus(device_index=device_index, status=UNHEALTHY)

        temp = float(raw.get("temperature", 0.0))
        util = float(raw.get("utilization", 0.0))
        mem_used = int(raw.get("memory_used", 0))
        mem_total = int(raw.get("memory_total", 0))
        ecc = int(raw.get("ecc_errors", 0))
        status = self._evaluate_status(temp, ecc)
        return GPUHealthStatus(
            device_index=device_index,
            status=status,
            temperature=temp,
            utilization=util,
            memory_used=mem_used,
            memory_total=mem_total,
            ecc_errors=ecc,
        )

    async def check_all(self) -> list[GPUHealthStatus]:
        """检查所有 GPU（``device_count`` 个），返回状态列表。"""
        return [await self.check_device(i) for i in range(self._device_count)]

    async def monitor_loop(
        self,
        interval: float,
        callback: MonitorCallback | None = None,
        *,
        max_iterations: int | None = None,
    ) -> None:
        """周期性监控循环。

        Args:
            interval: 轮询间隔（秒）。
            callback: 每轮调用的异步回调，接收本轮所有设备健康状态列表。
            max_iterations: 最大迭代次数；``None`` 表示无限循环
                （由调用方通过 ``asyncio.CancelledError`` 取消）。
        """
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            statuses = await self.check_all()
            if callback is not None:
                await callback(statuses)
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            await asyncio.sleep(interval)

    def _evaluate_status(self, temperature: float, ecc_errors: int) -> str:
        """根据温度与 ECC 错误评估健康状态。

        优先级：ECC 超阈值 → ``Unhealthy`` > 温度超阈值 → ``Degraded`` > ``Healthy``。
        """
        if ecc_errors > self._ecc_threshold:
            return UNHEALTHY
        if temperature > self._temp_threshold:
            return DEGRADED
        return HEALTHY


__all__ = [
    "GPUHealthStatus",
    "GPUHealthChecker",
    "DeviceCheckFn",
    "MonitorCallback",
    "HEALTHY",
    "DEGRADED",
    "UNHEALTHY",
]
