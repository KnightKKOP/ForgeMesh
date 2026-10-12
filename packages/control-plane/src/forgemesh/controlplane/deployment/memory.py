"""启动器的测试替身：在内存中记录实例，不启动进程、不提供服务。"""

from __future__ import annotations

import itertools

from forgemesh.resource.inference import EndpointProtocol

from .launcher import LaunchedInstance, LaunchRequest, RuntimeLauncher


class InMemoryLauncher(RuntimeLauncher):
    """记录启动与停止的启动器测试替身。

    实例只是内存中的记录，地址 ``http://<instance-id>.launcher.invalid`` 不可解析
    （``.invalid`` 见 RFC 2606），用于测试控制器与工作流；需要能接收请求的实例时使用其他
    启动器。同一个对象跨“重启”复用，相当于实例在控制平面重启后仍在运行。

    Args:
        protocol: 实例的协议。

    Attributes:
        prepared: 每次 ``prepare`` 调用的请求。
        launches: 每次 ``launch`` 调用的请求（含返回已有实例的调用）。
        stops: 每次 ``stop`` 调用的实例 id（含实例已停止的调用）。
    """

    def __init__(self, protocol: EndpointProtocol = EndpointProtocol.OPENAI_CHAT) -> None:
        self._protocol = protocol
        self._ids = itertools.count(1)
        self._instances: dict[str, LaunchedInstance] = {}
        self.prepared: list[LaunchRequest] = []
        self.launches: list[LaunchRequest] = []
        self.stops: list[str] = []

    @property
    def running(self) -> dict[str, LaunchedInstance]:
        """正在运行的实例（按实例 id）。"""
        return {instance.instance_id: instance for instance in self._instances.values()}

    async def prepare(self, request: LaunchRequest) -> None:
        self.prepared.append(request)

    async def launch(self, request: LaunchRequest) -> LaunchedInstance:
        """返回 ``instance_key`` 对应的运行中实例，没有时创建一个。"""
        self.launches.append(request)
        instance = self._instances.get(request.instance_key)
        if instance is None:
            instance_id = f"instance-{next(self._ids)}"
            instance = LaunchedInstance(
                instance_id=instance_id,
                url=f"http://{instance_id}.launcher.invalid",
                protocol=self._protocol,
            )
            self._instances[request.instance_key] = instance
        return instance

    async def verify(self, instance: LaunchedInstance) -> None:
        """实例不在运行时抛出 :class:`RuntimeError`。"""
        if instance.instance_id not in self.running:
            raise RuntimeError(f"instance {instance.instance_id} is not running")

    async def stop(self, instance_id: str) -> None:
        self.stops.append(instance_id)
        self._instances = {
            key: instance
            for key, instance in self._instances.items()
            if instance.instance_id != instance_id
        }

    async def is_running(self, instance_id: str) -> bool:
        return instance_id in self.running


__all__ = ["InMemoryLauncher"]
