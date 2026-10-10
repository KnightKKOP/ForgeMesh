"""vLLM 推理服务部署：启动实例、绑定端口与资源、注册路由。

对齐 ``docs/architecture/development-plan.md`` 5.3 与 ADR-011（运行时版本固定）。
本模块负责：

* 根据部署配置启动 vLLM 推理实例（``deploy``）；
* 为实例注册请求路由与健康探针，返回 endpoint（``register_route``）；
* 查询与列举已部署实例。

架构不变量：

* **实例可启动并可路由**：``deploy`` 返回的实例状态为 ``RUNNING``，
  ``register_route`` 返回非空 endpoint。
* **资源绑定显式**：``DeployConfig.gpu_ids`` / ``tensor_parallel_size`` 显式声明
  占用的设备与并行度，不隐式抢占。
* **测试友好**：实例启动器（launcher）可注入，无真实 vLLM 进程时用 mock 即可测试。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

#: 实例启动器类型：给定配置与实例 id，返回启动后的 endpoint。
InstanceLauncher = Callable[["DeployConfig", str], Awaitable[str]]


class InstanceStatus(StrEnum):
    """vLLM 实例状态。

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


class DeployConfig(BaseModel):
    """vLLM 部署配置。

    Attributes:
        model_path: 模型制品本地路径。
        port: 服务监听端口。
        gpu_ids: 绑定的 GPU 设备 id 列表。
        tensor_parallel_size: 张量并行度。
        env: 额外环境变量。
        max_model_len: 模型最大上下文长度。
    """

    model_config = ConfigDict(extra="forbid")

    model_path: str
    port: int = Field(gt=0, lt=65536)
    gpu_ids: list[int] = Field(default_factory=list)
    tensor_parallel_size: int = Field(default=1, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    max_model_len: int = Field(default=4096, gt=0)


class VLLMInstance(BaseModel):
    """vLLM 实例。

    Attributes:
        instance_id: 实例唯一标识。
        config: 部署配置。
        status: 实例状态。
        endpoint: 服务端点（路由注册后填充）。
        started_at: 启动时间戳（Unix epoch 秒）。
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    config: DeployConfig
    status: InstanceStatus = InstanceStatus.STARTING
    endpoint: str | None = None
    started_at: float = Field(default_factory=time.time)


async def _default_launcher(config: DeployConfig, instance_id: str) -> str:
    """默认实例启动器：实际环境应拉起 vLLM 进程并返回 endpoint。

    未注入 launcher 时抛 ``NotImplementedError``，提示调用方注入 mock 或
    配置真实启动实现。
    """
    raise NotImplementedError(
        "default vLLM launcher is not configured; inject a launcher for production use",
    )


class VLLMDeployer:
    """vLLM 部署器：启动实例、注册路由、查询实例。

    Args:
        launcher: 实例启动器（可注入 mock）；默认抛 ``NotImplementedError``。
    """

    def __init__(self, launcher: InstanceLauncher | None = None) -> None:
        self._launcher: InstanceLauncher = launcher or _default_launcher
        self._instances: dict[str, VLLMInstance] = {}

    async def deploy(self, config: DeployConfig) -> VLLMInstance:
        """启动 vLLM 实例。

        通过注入的 launcher 拉起 vLLM 进程，创建实例记录并标记为
        ``RUNNING``，endpoint 暂为 ``None``（由 ``register_route`` 填充）。

        Args:
            config: 部署配置。

        Returns:
            启动后的 :class:`VLLMInstance`（状态 ``RUNNING``）。
        """
        instance_id = f"vllm-{uuid.uuid4().hex[:12]}"
        instance = VLLMInstance(
            instance_id=instance_id,
            config=config,
            status=InstanceStatus.STARTING,
        )
        self._instances[instance_id] = instance
        # 启动进程；launcher 返回的 endpoint 暂存于实例 endpoint 字段。
        endpoint = await self._launcher(config, instance_id)
        instance = instance.model_copy(
            update={"status": InstanceStatus.RUNNING, "endpoint": endpoint},
        )
        self._instances[instance_id] = instance
        return instance

    async def register_route(self, instance: VLLMInstance) -> str:
        """为实例注册请求路由与健康探针，返回 endpoint。

        将实例状态推进为 ``READY``，并返回其服务 endpoint。

        Args:
            instance: 待注册路由的实例。

        Returns:
            实例的服务 endpoint。

        Raises:
            ValueError: 实例 endpoint 为空（未启动）或实例不存在。
        """
        if instance.instance_id not in self._instances:
            raise ValueError(f"instance not found: {instance.instance_id}")
        if not instance.endpoint:
            raise ValueError(
                f"instance {instance.instance_id} has no endpoint; deploy may have failed",
            )
        ready_instance = instance.model_copy(update={"status": InstanceStatus.READY})
        self._instances[instance.instance_id] = ready_instance
        return ready_instance.endpoint

    async def get_instance(self, instance_id: str) -> VLLMInstance | None:
        """查询实例。

        Args:
            instance_id: 实例 id。

        Returns:
            实例；不存在返回 ``None``。
        """
        return self._instances.get(instance_id)

    async def list_instances(self) -> list[VLLMInstance]:
        """列出所有已部署实例。"""
        return list(self._instances.values())

    async def stop_instance(self, instance_id: str) -> VLLMInstance | None:
        """停止实例（状态推进为 ``STOPPED``）。

        供 ``release`` 模块调用；不在此处回收设备租约。

        Args:
            instance_id: 实例 id。

        Returns:
            停止后的实例；不存在返回 ``None``。
        """
        instance = self._instances.get(instance_id)
        if instance is None:
            return None
        stopped = instance.model_copy(update={"status": InstanceStatus.STOPPED})
        self._instances[instance_id] = stopped
        return stopped


__all__ = [
    "DeployConfig",
    "InstanceLauncher",
    "InstanceStatus",
    "VLLMDeployer",
    "VLLMInstance",
]
