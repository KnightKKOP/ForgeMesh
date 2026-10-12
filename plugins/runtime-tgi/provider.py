"""TGI (Text Generation Inference) 推理运行时 Provider。

HuggingFace TGI（Text Generation Inference）服务适配，实现推理 Runtime 契约：
部署 → 冒烟验证 → 健康检查 → 释放的完整生命周期闭环。

对齐 ``docs/architecture/development-plan.md`` 5.3 与 ADR-011（运行时版本固定）。
本模块负责：

* 根据部署配置启动 TGI 推理实例（``deploy``）；
* 冒烟验证服务可推理（``verify``）；
* 健康检查（``health_check``）；
* 资源回收（``release``：停止实例 + 释放租约 + 分配核验）。

TGI 特定参数：

* ``--model``：模型 id 或路径（``TgiDeployConfig.model_id``）。
* ``--sharded``：多 GPU 分片模式（``TgiDeployConfig.sharded``）。
* ``--quantize``：量化方式（``TgiDeployConfig.quantize``，如 ``bitsandbytes`` / ``gptq`` / ``awq``）。

架构不变量：

* **实例可启动并可路由**：``deploy`` 返回的实例状态为 ``RUNNING``，endpoint 非空。
* **资源绑定显式**：``ResourceAllocation.device_ids`` 显式声明占用的设备。
* **契约一致性**：与 runtime-vllm 行为一致（deploy → verify → release 闭环）。
* **测试友好**：实例启动器、HTTP 客户端、租约释放与分配核验函数均可注入 mock。

基于 Pydantic v2。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: 实例启动器类型：给定配置与实例 id，返回启动后的 endpoint。
InstanceLauncher = Callable[["TgiDeployConfig", str], Awaitable[str]]

#: HTTP 推理请求函数类型：给定 endpoint 与 prompt，返回响应 dict。
#:
#: 响应 dict 约定字段：``ok`` (bool)、``latency_ms`` (float)、``text`` (str)、``status`` (int)。
HttpClient = Callable[[str, str], Awaitable[dict[str, Any]]]

#: 租约释放函数类型：给定 lease_id，返回是否释放成功。
LeaseReleaser = Callable[[str], Awaitable[bool]]

#: 分配核验函数类型：给定设备 id 列表，返回设备是否均已释放。
AllocationVerifier = Callable[[list[int]], Awaitable[bool]]


class InstanceStatus(StrEnum):
    """TGI 实例状态。

    Attributes:
        STARTING: 启动中。
        RUNNING: 进程已运行（尚未就绪）。
        READY: 已就绪可服务。
        STOPPING: 停止中。
        STOPPED: 已停止。
        FAILED: 启动或运行失败。
    """

    STARTING = "Starting"
    RUNNING = "Running"
    READY = "Ready"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    FAILED = "Failed"


class TgiDeployConfig(BaseModel):
    """TGI 部署配置。

    Attributes:
        model_id: 模型 id 或路径（对应 TGI ``--model`` 参数）。
        port: 服务监听端口。
        gpu_ids: 绑定的 GPU 设备 id 列表。
        sharded: 是否启用多 GPU 分片模式（对应 TGI ``--sharded`` 参数）。
        quantize: 量化方式（对应 TGI ``--quantize`` 参数，
            如 ``bitsandbytes`` / ``gptq`` / ``awq``）；``None`` 表示不量化。
        max_input_length: 最大输入 token 长度。
        max_total_tokens: 最大总 token 长度（输入 + 输出）。
        env: 额外环境变量。
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    port: int = Field(gt=0, lt=65536)
    gpu_ids: list[int] = Field(default_factory=list)
    sharded: bool = Field(default=False, description="多 GPU 分片模式（--sharded）")
    quantize: str | None = Field(default=None, description="量化方式（--quantize）")
    max_input_length: int = Field(default=4096, gt=0)
    max_total_tokens: int = Field(default=8192, gt=0)
    env: dict[str, str] = Field(default_factory=dict)


class ResourceAllocation(BaseModel):
    """资源分配（部署时传入，声明占用的计算资源）。

    Attributes:
        lease_id: 资源租约 id（释放时用于归还资源池）。
        device_ids: 占用的设备 id 列表。
        memory_mb: 分配的显存（MB），``0`` 表示未限定。
    """

    model_config = ConfigDict(extra="forbid")

    lease_id: str
    device_ids: list[int] = Field(default_factory=list)
    memory_mb: int = Field(default=0, ge=0)


class TgiInstance(BaseModel):
    """TGI 实例。

    Attributes:
        instance_id: 实例唯一标识。
        config: 部署配置。
        allocation: 资源分配。
        status: 实例状态。
        endpoint: 服务端点（启动后填充）。
        started_at: 启动时间戳（Unix epoch 秒）。
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    config: TgiDeployConfig
    allocation: ResourceAllocation
    status: InstanceStatus = InstanceStatus.STARTING
    endpoint: str | None = None
    started_at: float = Field(default_factory=time.time)


class VerifyResult(BaseModel):
    """冒烟验证结果。

    Attributes:
        passed: 是否通过验证。
        latency_ms: 实测延迟（毫秒）。
        response: 冒烟推理响应文本。
        details: 诊断详情。
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool = False
    latency_ms: float = 0.0
    response: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class HealthStatus(BaseModel):
    """健康检查结果。

    Attributes:
        healthy: 是否健康。
        endpoint: 检查的端点。
        details: 诊断详情。
    """

    model_config = ConfigDict(extra="forbid")

    healthy: bool = False
    endpoint: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


async def _default_launcher(config: TgiDeployConfig, instance_id: str) -> str:
    """默认实例启动器：实际环境应拉起 TGI 进程并返回 endpoint。

    未注入 launcher 时抛 ``NotImplementedError``，提示调用方注入 mock 或
    配置真实启动实现。
    """
    raise NotImplementedError(
        "default TGI launcher is not configured; inject a launcher for production use",
    )


async def _default_http_client(endpoint: str, prompt: str) -> dict[str, Any]:
    """默认 HTTP 客户端：实际环境应使用 httpx/aiohttp 发起推理请求。

    未注入 client 时抛 ``NotImplementedError``。
    """
    raise NotImplementedError(
        "default HTTP client is not configured; inject a client for production use",
    )


async def _default_release_lease(lease_id: str) -> bool:
    """默认租约释放：实际环境应对接资源调度器。"""
    raise NotImplementedError(
        "default lease releaser is not configured; inject a releaser for production use",
    )


async def _default_verify_allocation(device_ids: list[int]) -> bool:
    """默认分配核验：实际环境应查询设备实际占用状态。"""
    raise NotImplementedError(
        "default allocation verifier is not configured; inject a verifier for production use",
    )


class TgiRuntimeProvider:
    """TGI 推理运行时 Provider。

    实现 deploy → verify → release 闭环，与 runtime-vllm 行为一致。

    Args:
        launcher: 实例启动器（可注入 mock）；默认抛 ``NotImplementedError``。
        http_client: HTTP 推理请求函数（可注入 mock）；默认抛 ``NotImplementedError``。
        lease_releaser: 租约释放函数（可注入 mock）。
        allocation_verifier: 分配核验函数（可注入 mock）。
    """

    def __init__(
        self,
        *,
        launcher: InstanceLauncher | None = None,
        http_client: HttpClient | None = None,
        lease_releaser: LeaseReleaser | None = None,
        allocation_verifier: AllocationVerifier | None = None,
    ) -> None:
        self._launcher: InstanceLauncher = launcher or _default_launcher
        self._http: HttpClient = http_client or _default_http_client
        self._release_lease_fn: LeaseReleaser = (
            lease_releaser or _default_release_lease
        )
        self._verify_allocation_fn: AllocationVerifier = (
            allocation_verifier or _default_verify_allocation
        )
        self._instances: dict[str, TgiInstance] = {}

    async def deploy(
        self,
        model_config: TgiDeployConfig,
        resource_allocation: ResourceAllocation,
    ) -> TgiInstance:
        """部署 TGI 实例。

        通过注入的 launcher 拉起 TGI 进程，创建实例记录并标记为 ``RUNNING``。

        Args:
            model_config: TGI 部署配置（含 ``--model`` / ``--sharded`` / ``--quantize`` 参数）。
            resource_allocation: 资源分配（设备与租约）。

        Returns:
            启动后的 :class:`TgiInstance`（状态 ``RUNNING``）。
        """
        instance_id = f"tgi-{uuid.uuid4().hex[:12]}"
        instance = TgiInstance(
            instance_id=instance_id,
            config=model_config,
            allocation=resource_allocation,
            status=InstanceStatus.STARTING,
        )
        self._instances[instance_id] = instance
        endpoint = await self._launcher(model_config, instance_id)
        instance = instance.model_copy(
            update={"status": InstanceStatus.RUNNING, "endpoint": endpoint},
        )
        self._instances[instance_id] = instance
        return instance

    async def verify(self, instance: TgiInstance) -> VerifyResult:
        """冒烟验证：向实例发送推理请求，验证服务可用。

        Args:
            instance: 待验证的 TGI 实例（须已部署且有 endpoint）。

        Returns:
            :class:`VerifyResult`，``passed=True`` 表示冒烟通过。
        """
        if not instance.endpoint:
            return VerifyResult(
                passed=False,
                details={"error": "instance has no endpoint"},
            )
        try:
            resp = await self._http(instance.endpoint, "Hello, who are you?")
        except Exception as exc:
            return VerifyResult(
                passed=False,
                details={"error": str(exc)},
            )
        ok = bool(resp.get("ok", False))
        return VerifyResult(
            passed=ok,
            latency_ms=float(resp.get("latency_ms", 0.0)),
            response=str(resp.get("text", "")),
            details={"status": resp.get("status", 0), "ok": ok},
        )

    async def release(self, instance: TgiInstance) -> None:
        """释放 TGI 实例：停止实例 + 释放租约 + 分配核验。

        Args:
            instance: 待释放的 TGI 实例。

        Raises:
            ValueError: 实例不存在或释放失败。
        """
        stored = self._instances.get(instance.instance_id)
        if stored is None:
            raise ValueError(f"instance not found: {instance.instance_id}")

        # 1. 停止实例。
        stopped = stored.model_copy(update={"status": InstanceStatus.STOPPED})
        self._instances[instance.instance_id] = stopped

        # 2. 释放租约。
        lease_ok = await self._release_lease_fn(instance.allocation.lease_id)
        if not lease_ok:
            raise ValueError(
                f"lease release failed for {instance.allocation.lease_id}",
            )

        # 3. 分配核验（确认设备实际已释放，不虚占）。
        alloc_ok = await self._verify_allocation_fn(instance.allocation.device_ids)
        if not alloc_ok:
            raise ValueError(
                f"allocation verification failed for devices {instance.allocation.device_ids}",
            )

    async def health_check(self, instance: TgiInstance) -> HealthStatus:
        """健康检查：向实例发送轻量请求，判断服务是否健康。

        Args:
            instance: 待检查的 TGI 实例。

        Returns:
            :class:`HealthStatus`，``healthy=True`` 表示服务健康。
        """
        if not instance.endpoint:
            return HealthStatus(
                healthy=False,
                endpoint="",
                details={"error": "instance has no endpoint"},
            )
        try:
            resp = await self._http(instance.endpoint, "")
        except Exception as exc:
            return HealthStatus(
                healthy=False,
                endpoint=instance.endpoint,
                details={"error": str(exc)},
            )
        ok = bool(resp.get("ok", False))
        return HealthStatus(
            healthy=ok,
            endpoint=instance.endpoint,
            details={"status": resp.get("status", 0)},
        )

    # ---- 查询辅助方法 ----

    async def get_instance(self, instance_id: str) -> TgiInstance | None:
        """查询实例。

        Args:
            instance_id: 实例 id。

        Returns:
            实例；不存在返回 ``None``。
        """
        return self._instances.get(instance_id)

    async def list_instances(self) -> list[TgiInstance]:
        """列出所有已部署实例。"""
        return list(self._instances.values())


__all__ = [
    "AllocationVerifier",
    "HealthStatus",
    "HttpClient",
    "InstanceLauncher",
    "InstanceStatus",
    "LeaseReleaser",
    "ResourceAllocation",
    "TgiDeployConfig",
    "TgiInstance",
    "TgiRuntimeProvider",
    "VerifyResult",
]
