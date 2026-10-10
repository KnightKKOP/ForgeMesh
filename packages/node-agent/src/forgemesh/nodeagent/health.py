"""健康检查与心跳（WP-3.1）。

:class:`HealthChecker` 执行节点健康检查，返回
:class:`~forgemesh.resource.node.HealthReport`；支持注入 ``check_func`` 以在测试
中替换真实检查逻辑。

:class:`HeartbeatSender` 周期上报心跳及在途执行列表，供控制平面做离线检测与
断线恢复（对齐 architecture 10.3 / INV-4）。``start_loop`` 默认无限循环，测试时
可通过 ``max_iterations`` 控制迭代次数，避免阻塞。

:class:`MetricsCollector` 采集利用率/显存等指标，供心跳附带上报。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from forgemesh.resource.node import HealthReport

if TYPE_CHECKING:
    from forgemesh.nodeagent.server import AgentServer


#: 健康检查函数签名。
CheckFunc = Callable[[], Awaitable[HealthReport]]
#: 指标采集函数签名。
CollectFunc = Callable[[], dict[str, Any]]
#: 在途执行获取函数签名。
GetRunningFunc = Callable[[], list[str]]


class HealthChecker:
    """健康检查器。

    Args:
        check_func: 注入的检查协程；``None`` 时使用默认检查（返回 ``Healthy``）。
    """

    def __init__(self, *, check_func: CheckFunc | None = None) -> None:
        self._check_func = check_func

    async def check(self) -> HealthReport:
        """检查节点健康，返回 :class:`HealthReport`。"""
        if self._check_func is not None:
            return await self._check_func()
        return HealthReport(
            status="Healthy",
            observed_at=datetime.now(UTC),
        )


class MetricsCollector:
    """指标采集器。

    Args:
        collect_func: 注入的采集函数；``None`` 时返回空指标占位。
    """

    def __init__(self, *, collect_func: CollectFunc | None = None) -> None:
        self._collect_func = collect_func

    def collect(self) -> dict[str, Any]:
        """采集利用率/显存等指标，返回 dict。"""
        if self._collect_func is not None:
            return self._collect_func()
        return {"gpu_utilization": [], "memory_used_bytes": 0}


class HeartbeatSender:
    """心跳发送。

    节点侧周期上报 ``agent_id`` / ``agent_version`` 及在途执行列表，
    供控制平面做离线检测与断线恢复决策。
    """

    async def send(
        self,
        server: AgentServer,
        *,
        agent_id: str,
        agent_version: str,
        running_executions: list[str],
    ) -> bool:
        """发送一次心跳；未注册 agent 返回 ``False``。"""
        return await server.receive_heartbeat(
            agent_id=agent_id,
            agent_version=agent_version,
            running_executions=running_executions,
        )

    async def start_loop(
        self,
        server: AgentServer,
        *,
        agent_id: str,
        agent_version: str,
        interval: int,
        get_running: GetRunningFunc,
        max_iterations: int | None = None,
    ) -> None:
        """启动周期心跳循环。

        Args:
            interval: 心跳间隔（秒）。
            get_running: 返回当前在途 execution_id 列表的可调用对象。
            max_iterations: 最大迭代次数；``None`` 表示无限循环。测试时可设置
                有限值以避免阻塞。
        """
        count = 0
        while max_iterations is None or count < max_iterations:
            await self.send(
                server,
                agent_id=agent_id,
                agent_version=agent_version,
                running_executions=get_running(),
            )
            count += 1
            await asyncio.sleep(interval)


__all__ = [
    "HealthChecker",
    "MetricsCollector",
    "HeartbeatSender",
]
