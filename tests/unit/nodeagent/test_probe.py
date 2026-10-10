"""``forgemesh.nodeagent.probe`` 单元测试。

覆盖设备/驱动探测与快照上报：

* probe 返回注入的设备列表（accelerators / device_instances / topology）；
* snapshot 结构正确（node / accelerators / device_instances / topology 键齐全）；
* SnapshotReporter.report 上报成功/失败（未注册 agent）。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.nodeagent.probe import DeviceProber, ProbeResult, SnapshotReporter
from forgemesh.nodeagent.server import AgentServer
from forgemesh.resource.node import Accelerator, DeviceInstance, Topology

pytestmark = pytest.mark.unit


async def _fake_probe() -> ProbeResult:
    """注入的探测协程：返回两个加速器与一个设备实例。"""
    return ProbeResult(
        accelerators=[
            Accelerator(vendor="nvidia", model="H100", memory_bytes=80 * 1024**3),
            Accelerator(vendor="nvidia", model="H100", memory_bytes=80 * 1024**3),
        ],
        device_instances=[
            DeviceInstance(profile="h100-full", health="Healthy"),
        ],
        topology=Topology(nodes=["gpu-0", "gpu-1"], discovery="nvlink"),
    )


def test_probe_returns_devices() -> None:
    """注入 probe_func 时，probe 应返回其探测结果。"""
    prober = DeviceProber(probe_func=_fake_probe, agent_version="0.1.0")

    result = asyncio.run(prober.probe())

    assert len(result.accelerators) == 2
    assert result.accelerators[0].model == "H100"
    assert len(result.device_instances) == 1
    assert result.device_instances[0].health == "Healthy"
    assert result.topology.nodes == ["gpu-0", "gpu-1"]


def test_probe_default_returns_empty() -> None:
    """未注入 probe_func 时，默认探测返回空结果。"""
    prober = DeviceProber()

    result = asyncio.run(prober.probe())

    assert result.accelerators == []
    assert result.device_instances == []
    assert result.topology.nodes == []


def test_snapshot_structure() -> None:
    """snapshot 应包含 node/accelerators/device_instances/topology 四个键。"""
    prober = DeviceProber(probe_func=_fake_probe, agent_version="0.1.0")

    snap = asyncio.run(prober.snapshot())

    assert set(snap.keys()) == {"node", "accelerators", "device_instances", "topology"}
    assert snap["node"]["agent_version"] == "0.1.0"
    assert snap["node"]["last_seen_at"] is not None
    assert len(snap["accelerators"]) == 2
    assert snap["accelerators"][0]["model"] == "H100"
    assert len(snap["device_instances"]) == 1
    assert snap["topology"]["discovery"] == "nvlink"


def test_report_success() -> None:
    """已注册 agent 上报快照应成功。"""
    server = AgentServer()
    asyncio.run(
        server.register(
            agent_id="agent-1",
            agent_version="0.1.0",
            capabilities=["nvidia-gpu"],
            supported_task_types=["PROBE_GPU"],
            core_api_version="v1",
        )
    )
    reporter = SnapshotReporter(agent_id="agent-1")

    ok = asyncio.run(reporter.report(server, {"node": {"health": "Healthy"}}))

    assert ok is True


def test_report_unregistered_agent() -> None:
    """未注册 agent 上报快照应返回 False。"""
    server = AgentServer()
    reporter = SnapshotReporter(agent_id="ghost")

    ok = asyncio.run(reporter.report(server, {"node": {}}))

    assert ok is False
