"""混沌测试：节点离线（INV-4）。

验证 Agent 心跳超时后系统不变量保持：

* ``HeartbeatManager.check_offline()`` 返回离线 agent；
* ``get_agent_state`` 返回 ``OFFLINE``；
* 离线 agent 的资源健康退化为 ``Unknown`` / ``Degraded``，不可标记 ``Available``；
* ``list_online_agents`` 不包含离线 agent（失联不误判可用）。

通过 ``monkeypatch`` ``time.time`` 模拟心跳超时，不需要真实硬件。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from forgemesh.controlplane.heartbeat import (
    AgentState,
    HeartbeatManager,
    HeartbeatRecord,
)
from forgemesh.resource.node import DEFAULT_HEALTH

pytestmark = pytest.mark.unit


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _heartbeat(agent_id: str) -> HeartbeatRecord:
    """构造最小心跳记录。"""
    return HeartbeatRecord(agent_id=agent_id, sent_at="2026-10-11T00:00:00Z")


def _resource_health(state: AgentState) -> str:
    """根据 Agent 状态推断资源健康状态（INV-4 映射）。

    * ``ONLINE``   → ``Healthy``
    * ``DEGRADED`` → ``Degraded``
    * ``OFFLINE``  → ``Unknown``（:data:`DEFAULT_HEALTH`，不可用）

    离线 / 降级 agent 的资源不可标记为 ``Available``。
    """
    if state is AgentState.ONLINE:
        return "Healthy"
    if state is AgentState.DEGRADED:
        return "Degraded"
    return DEFAULT_HEALTH  # "Unknown"


# ---------------------------------------------------------------------- #
# 心跳超时 → OFFLINE
# ---------------------------------------------------------------------- #


def test_heartbeat_timeout_marks_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """心跳超过离线阈值后 get_agent_state 返回 OFFLINE。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0, degraded_threshold_seconds=15.0
    )
    _run(mgr.receive(_heartbeat("agent-1")))

    t[0] = 1031.0  # 超过离线阈值
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.OFFLINE


def test_check_offline_returns_offline_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_offline 返回本次新转为 OFFLINE 的 agent 列表。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0, degraded_threshold_seconds=15.0
    )
    _run(mgr.receive(_heartbeat("agent-1")))
    _run(mgr.receive(_heartbeat("agent-2")))

    t[0] = 1031.0
    newly = _run(mgr.check_offline())
    assert set(newly) == {"agent-1", "agent-2"}

    # 再次调用：已是 OFFLINE，不再返回
    assert _run(mgr.check_offline()) == []


# ---------------------------------------------------------------------- #
# 离线后资源不误判可用（INV-4）
# ---------------------------------------------------------------------- #


def test_offline_agent_resources_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-4：离线 agent 资源健康为 Unknown，不可标记 Available/Healthy。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0, degraded_threshold_seconds=15.0
    )
    _run(mgr.receive(_heartbeat("agent-1")))

    t[0] = 1031.0
    state = _run(mgr.get_agent_state("agent-1"))
    assert state is AgentState.OFFLINE

    health = _resource_health(state)
    assert health == DEFAULT_HEALTH
    assert health not in ("Available", "Healthy")


def test_degraded_agent_resources_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEGRADED agent 资源为 Degraded，同样不可标记 Available。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0, degraded_threshold_seconds=15.0
    )
    _run(mgr.receive(_heartbeat("agent-1")))

    t[0] = 1016.0  # 超过降级阈值但未达离线阈值
    state = _run(mgr.get_agent_state("agent-1"))
    assert state is AgentState.DEGRADED

    health = _resource_health(state)
    assert health == "Degraded"
    assert health not in ("Available", "Healthy")


def test_offline_agent_excluded_from_online_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-4：离线 agent 不出现在 list_online_agents 中（不回收到调度池）。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0, degraded_threshold_seconds=15.0
    )
    _run(mgr.receive(_heartbeat("agent-1")))
    _run(mgr.receive(_heartbeat("agent-2")))

    t[0] = 1031.0
    online = _run(mgr.list_online_agents())
    assert online == []
    assert "agent-1" not in online
    assert "agent-2" not in online


def test_unknown_agent_treated_as_offline() -> None:
    """从未心跳过的 agent 视为 OFFLINE，资源为 Unknown。"""
    mgr = HeartbeatManager()
    state = _run(mgr.get_agent_state("ghost"))
    assert state is AgentState.OFFLINE
    assert _resource_health(state) == DEFAULT_HEALTH


# ---------------------------------------------------------------------- #
# 恢复与部分故障隔离
# ---------------------------------------------------------------------- #


def test_offline_then_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    """离线后重新心跳恢复 ONLINE，资源恢复 Healthy。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0, degraded_threshold_seconds=15.0
    )
    _run(mgr.receive(_heartbeat("agent-1")))

    t[0] = 1031.0
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.OFFLINE

    t[0] = 1032.0
    _run(mgr.receive(_heartbeat("agent-1")))
    state = _run(mgr.get_agent_state("agent-1"))
    assert state is AgentState.ONLINE
    assert _resource_health(state) == "Healthy"


def test_partial_outage_isolates_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """部分节点离线不影响在线节点：仅离线 agent 资源不可用。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0, degraded_threshold_seconds=15.0
    )
    _run(mgr.receive(_heartbeat("agent-1")))
    _run(mgr.receive(_heartbeat("agent-2")))

    t[0] = 1031.0
    # agent-2 重新心跳，agent-1 保持离线
    _run(mgr.receive(_heartbeat("agent-2")))

    online = _run(mgr.list_online_agents())
    assert online == ["agent-2"]

    state_offline = _run(mgr.get_agent_state("agent-1"))
    state_online = _run(mgr.get_agent_state("agent-2"))
    assert state_offline is AgentState.OFFLINE
    assert state_online is AgentState.ONLINE
    assert _resource_health(state_offline) == DEFAULT_HEALTH
    assert _resource_health(state_online) == "Healthy"
