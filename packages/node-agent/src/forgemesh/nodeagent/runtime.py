"""本地推理运行时管理（WP-3.1）。

:class:`RuntimeManager` 管理本地推理运行时实例（如 vllm/tensorrt-llm）的启动、
停止、列举与查询。实例以 ``endpoint``（``http://localhost:{port}``）作为唯一
``instance_id``，与 :class:`~forgemesh.resource.node.RuntimeInstance` 的端点字段
一一对应，便于上层通过端点寻址。

本模块为内存管理器（不拉起真实进程），适合在 Node Agent 内部维护运行时拓扑并
向控制平面上报。对齐 architecture 10.1（Node Agent 受控执行）与 ADR-011（版本固定）。
兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.resource.node import RuntimeInstance
from pydantic import BaseModel, ConfigDict, Field


class RuntimeConfig(BaseModel):
    """运行时配置。

    Attributes:
        runtime: 运行时标识（如 ``vllm``）。
        model_path: 模型路径（本地或对象存储）。
        port: 服务监听端口。
        gpu_ids: 绑定的 GPU 索引列表。
        env: 启动环境变量。
    """

    model_config = ConfigDict(extra="forbid")

    runtime: str
    model_path: str
    port: int = Field(ge=1, le=65535)
    gpu_ids: list[int] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class RuntimeManager:
    """管理本地推理运行时实例。

    ``instance_id`` 即实例的 ``endpoint``（``http://localhost:{port}``），唯一标识。
    同端口重复启动抛 :class:`~forgemesh.core.errors.ConflictError`；
    停止/查询不存在的实例抛 :class:`~forgemesh.core.errors.NotFoundError`（停止）
    或返回 ``None``（查询）。
    """

    def __init__(self) -> None:
        self._instances: dict[str, RuntimeInstance] = {}

    async def start_instance(self, config: RuntimeConfig) -> RuntimeInstance:
        """启动运行时实例，返回 :class:`RuntimeInstance`。"""
        endpoint = f"http://localhost:{config.port}"
        if endpoint in self._instances:
            raise ConflictError(f"runtime instance already running at {endpoint}")
        instance = RuntimeInstance(
            runtime=config.runtime,
            endpoint=endpoint,
            health="Unknown",
        )
        self._instances[endpoint] = instance
        return instance

    async def stop_instance(self, instance_id: str) -> None:
        """停止实例；不存在抛 :class:`NotFoundError`。"""
        if instance_id not in self._instances:
            raise NotFoundError(f"runtime instance not found: {instance_id}")
        del self._instances[instance_id]

    async def list_instances(self) -> list[RuntimeInstance]:
        """列出运行中实例。"""
        return list(self._instances.values())

    async def get_instance(self, instance_id: str) -> RuntimeInstance | None:
        """查询实例；不存在返回 ``None``。"""
        return self._instances.get(instance_id)


__all__ = [
    "RuntimeConfig",
    "RuntimeManager",
]
