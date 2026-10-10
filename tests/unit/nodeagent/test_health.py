"""``forgemesh.nodeagent.health`` 单元测试。

覆盖健康检查、心跳与指标采集：

* HealthChecker.check 返回 HealthReport（默认/注入）；
* HeartbeatSender.send 心跳成功/失败（未注册 agent）；
* HeartbeatSender.start_loop 周期心跳（max_iterations 控制）；
* MetricsCollector.collect 返回指标（默认/注入）。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.nodeagent.health import HealthChecker, HeartbeatSender, MetricsCollector
from forgemesh.nodeagent.server import AgentServer
from forgemesh.resource.node import HealthReport

pytestmark = pytest.mark.unit


async def _degraded_check() -> HealthReport:
    """注入的健康检查协程：返回 Degraded。"""
    return HealthReport(status="Degraded", message="gpu thermal throttling")


def _register(server: AgentServer, agent_id: str = "agent-1") -> None:
    """在 server 上注册一个 agent。"""
    asyncio.run(
        server.register(
            agent_id=agent_id,
            agent_version="0.1.0",
            capabilities=["nvidia-gpu"],
            supported_task_types=["PROBE_GPU"],
            core_api_version="v1",
        )
    )


def test_check_default_healthy() -> None:
    """默认 check 应返回 Healthy。"""
    checker = HealthChecker()

    report = asyncio.run(checker.check())

    assert report.status == "Healthy"


def test_check_injected() -> None:
    """注入 check_func 时应返回其结果。"""
    checker = HealthChecker(check_func=_degraded_check)

    report = asyncio.run(checker.check())

    assert report.status == "Degraded"
    assert "throttling" in report.message


def test_heartbeat_send_success() -> None:
    """已注册 agent 发送心跳应成功。"""
    server = AgentServer()
    _register(server)
    sender = HeartbeatSender()

    ok = asyncio.run(
        sender.send(
            server,
            agent_id="agent-1",
            agent_version="0.1.0",
            running_executions=["exec-1"],
        )
    )

    assert ok is True


def test_heartbeat_send_unregistered() -> None:
    """未注册 agent 发送心跳应返回 False。"""
    server = AgentServer()
    sender = HeartbeatSender()

    ok = asyncio.run(
        sender.send(
            server,
            agent_id="ghost",
            agent_version="0.1.0",
            running_executions=[],
        )
    )

    assert ok is False


def test_heartbeat_loop() -> None:
    """start_loop 在 max_iterations 内应发送指定次数心跳。"""
    server = AgentServer()
    _register(server)
    sender = HeartbeatSender()
    calls: list[str] = []

    def get_running() -> list[str]:
        calls.append("tick")
        return ["exec-1"]

    asyncio.run(
        sender.start_loop(
            server,
            agent_id="agent-1",
            agent_version="0.1.0",
            interval=0,
            get_running=get_running,
            max_iterations=3,
        )
    )

    assert len(calls) == 3


def test_collect_default() -> None:
    """默认 collect 应返回占位指标 dict。"""
    collector = MetricsCollector()

    metrics = collector.collect()

    assert isinstance(metrics, dict)
    assert "gpu_utilization" in metrics


def test_collect_injected() -> None:
    """注入 collect_func 时应返回其结果。"""
    collector = MetricsCollector(collect_func=lambda: {"gpu0": 0.99})

    metrics = collector.collect()

    assert metrics == {"gpu0": 0.99}
