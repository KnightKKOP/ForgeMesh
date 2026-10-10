"""``forgemesh.controlplane.heartbeat`` 单元测试。

覆盖 HeartbeatManager 的核心语义：

* receive 心跳 → ONLINE；
* 超时 → DEGRADED → OFFLINE（降级阈值 < 离线阈值）；
* check_offline 返回新转为 OFFLINE 的 agent_id 列表；
* list_online_agents 仅返回 ONLINE（失联不误判可用，INV-4）；
* 未知 Agent 状态为 OFFLINE；
* HeartbeatAck / HeartbeatRecord 模型往返。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
通过 monkeypatch ``time.time`` 控制时间推进，精确验证阈值边界。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from forgemesh.controlplane.heartbeat import (
    AgentState,
    HeartbeatAck,
    HeartbeatManager,
    HeartbeatRecord,
)


def _make_record(
    agent_id: str = "agent-1",
    agent_version: str = "0.1.0",
    core_api_version: str = "v1alpha1",
    running_execution_ids: list[str] | None = None,
    sent_at: str = "2026-10-11T00:00:00Z",
) -> HeartbeatRecord:
    """构造一个可复用的最小 HeartbeatRecord。"""
    return HeartbeatRecord(
        agent_id=agent_id,
        agent_version=agent_version,
        core_api_version=core_api_version,
        running_execution_ids=running_execution_ids or [],
        sent_at=sent_at,
    )


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# 模型
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_agent_state_has_three_states() -> None:
    """AgentState 含 ONLINE / DEGRADED / OFFLINE 三态。"""
    members = {m.name for m in AgentState}
    assert members == {"ONLINE", "DEGRADED", "OFFLINE"}


@pytest.mark.unit
def test_heartbeat_record_defaults() -> None:
    """HeartbeatRecord 必填仅 agent_id，其余有默认。"""
    rec = HeartbeatRecord(agent_id="a")
    assert rec.agent_id == "a"
    assert rec.agent_version == ""
    assert rec.core_api_version == ""
    assert rec.running_execution_ids == []
    assert rec.sent_at == ""


@pytest.mark.unit
def test_heartbeat_ack_defaults() -> None:
    """HeartbeatAck 默认 accepted=True。"""
    ack = HeartbeatAck()
    assert ack.accepted is True
    assert ack.control_plane_time == ""


# ---------------------------------------------------------------------- #
# receive → ONLINE
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_receive_returns_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """接收心跳后 Agent 状态为 ONLINE。"""
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    mgr = HeartbeatManager()

    ack = _run(mgr.receive(_make_record("agent-1")))
    assert ack.accepted is True

    state = _run(mgr.get_agent_state("agent-1"))
    assert state is AgentState.ONLINE


@pytest.mark.unit
def test_receive_updates_running_executions(monkeypatch: pytest.MonkeyPatch) -> None:
    """receive 记录在途执行列表（重连恢复用）。"""
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    mgr = HeartbeatManager()

    _run(mgr.receive(_make_record("agent-1", running_execution_ids=["exec-a", "exec-b"])))
    tracker = mgr._agents["agent-1"]
    assert tracker.running_execution_ids == ["exec-a", "exec-b"]


# ---------------------------------------------------------------------- #
# 超时 → DEGRADED → OFFLINE
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_degraded_after_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 degraded_threshold 未心跳 → DEGRADED。"""
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )
    _run(mgr.receive(_make_record("agent-1")))

    # 推进 16 秒 → 超过降级阈值
    monkeypatch.setattr(time, "time", lambda: 1016.0)
    state = _run(mgr.get_agent_state("agent-1"))
    assert state is AgentState.DEGRADED


@pytest.mark.unit
def test_offline_after_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 offline_threshold 未心跳 → OFFLINE。"""
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )
    _run(mgr.receive(_make_record("agent-1")))

    # 推进 31 秒 → 超过离线阈值
    monkeypatch.setattr(time, "time", lambda: 1031.0)
    state = _run(mgr.get_agent_state("agent-1"))
    assert state is AgentState.OFFLINE


@pytest.mark.unit
def test_degraded_then_offline_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """心跳间隔递增：ONLINE → DEGRADED → OFFLINE。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )

    _run(mgr.receive(_make_record("agent-1")))
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.ONLINE

    t[0] = 1016.0  # +16s → DEGRADED
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.DEGRADED

    t[0] = 1031.0  # +31s → OFFLINE
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.OFFLINE


@pytest.mark.unit
def test_recover_from_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEGRADED Agent 重新心跳后恢复 ONLINE。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )

    _run(mgr.receive(_make_record("agent-1")))
    t[0] = 1016.0
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.DEGRADED

    # 重新心跳
    t[0] = 1017.0
    _run(mgr.receive(_make_record("agent-1")))
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.ONLINE


# ---------------------------------------------------------------------- #
# check_offline
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_check_offline_returns_newly_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_offline 返回本次新转为 OFFLINE 的 agent_id 列表。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )
    _run(mgr.receive(_make_record("agent-1")))
    _run(mgr.receive(_make_record("agent-2")))

    # 推进 31 秒，两个 agent 都超时
    t[0] = 1031.0
    newly = _run(mgr.check_offline())
    assert set(newly) == {"agent-1", "agent-2"}

    # 再次调用：已是 OFFLINE，不再返回
    newly_again = _run(mgr.check_offline())
    assert newly_again == []


@pytest.mark.unit
def test_check_offline_skips_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_offline 不返回仍在线的 agent。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )
    _run(mgr.receive(_make_record("agent-1")))  # 会超时
    _run(mgr.receive(_make_record("agent-2")))

    t[0] = 1031.0  # agent-1 超时
    # agent-2 重新心跳
    _run(mgr.receive(_make_record("agent-2")))

    newly = _run(mgr.check_offline())
    assert newly == ["agent-1"]


# ---------------------------------------------------------------------- #
# list_online_agents（INV-4：失联不误判可用）
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_online_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_online_agents 仅返回 ONLINE 状态的 agent。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )
    _run(mgr.receive(_make_record("agent-1")))
    _run(mgr.receive(_make_record("agent-2")))
    _run(mgr.receive(_make_record("agent-3")))

    t[0] = 1016.0  # 全部 DEGRADED
    online = _run(mgr.list_online_agents())
    assert online == []

    # agent-2 重新心跳
    _run(mgr.receive(_make_record("agent-2")))
    online = _run(mgr.list_online_agents())
    assert online == ["agent-2"]


@pytest.mark.unit
def test_offline_agent_not_in_online_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-4：OFFLINE agent 不出现在 list_online_agents 中。"""
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    mgr = HeartbeatManager(
        offline_threshold_seconds=30.0,
        degraded_threshold_seconds=15.0,
    )
    _run(mgr.receive(_make_record("agent-1")))
    _run(mgr.receive(_make_record("agent-2")))

    t[0] = 1031.0  # 全部 OFFLINE
    online = _run(mgr.list_online_agents())
    assert online == []
    assert _run(mgr.get_agent_state("agent-1")) is AgentState.OFFLINE


# ---------------------------------------------------------------------- #
# 未知 Agent
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_unknown_agent_state_is_offline() -> None:
    """从未心跳过的 Agent 视为 OFFLINE（INV-4）。"""
    mgr = HeartbeatManager()
    state = _run(mgr.get_agent_state("never-seen"))
    assert state is AgentState.OFFLINE


# ---------------------------------------------------------------------- #
# 构造校验
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_invalid_thresholds_raise() -> None:
    """非法阈值参数抛 ValueError。"""
    with pytest.raises(ValueError):
        HeartbeatManager(offline_threshold_seconds=0)
    with pytest.raises(ValueError):
        HeartbeatManager(degraded_threshold_seconds=0)
    with pytest.raises(ValueError):
        HeartbeatManager(
            offline_threshold_seconds=10.0,
            degraded_threshold_seconds=20.0,
        )
