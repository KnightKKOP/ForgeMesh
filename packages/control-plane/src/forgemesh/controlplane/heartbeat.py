"""gRPC 任务协议：心跳与离线检测。

对齐 ``api/protobuf/agent.proto`` Heartbeat / HeartbeatAck 与
development-plan 5.9 / INV-4。

设计要点
--------
* **内存存储**：心跳是临时状态，用 ``dict`` 内存存储，不落 SQLite。
* **三级状态**：:class:`AgentState` 含 ``ONLINE`` / ``DEGRADED`` / ``OFFLINE``。
  * 超过 ``degraded_threshold_seconds`` 未心跳 → ``DEGRADED``。
  * 超过 ``offline_threshold_seconds`` 未心跳 → ``OFFLINE``。
* **失联不误判可用**（INV-4）：离线 Agent 的资源不标记为可用。
  :meth:`HeartbeatManager.list_online_agents` 仅返回 ``ONLINE`` 状态的 Agent，
  ``DEGRADED`` / ``OFFLINE`` 均不视为可用，避免将失联 Agent 的资源回收到
  调度池。

兼容 Python 3.9+（运行环境 3.12，使用 :class:`enum.StrEnum`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------- #
# 枚举
# ---------------------------------------------------------------------- #


class AgentState(StrEnum):
    """Agent 连接状态枚举。

    * ``ONLINE`` —— 正常心跳，资源可调度。
    * ``DEGRADED`` —— 心跳延迟超过降级阈值，谨慎调度。
    * ``OFFLINE`` —— 心跳延迟超过离线阈值，资源不可用（INV-4）。
    """

    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


# ---------------------------------------------------------------------- #
# 心跳记录与确认
# ---------------------------------------------------------------------- #


class HeartbeatRecord(BaseModel):
    """Agent 心跳记录（对齐 ``agent.proto`` Heartbeat）。

    Attributes:
        agent_id: Agent 标识。
        agent_version: Agent 版本。
        core_api_version: Agent 支持的核心 API 版本。
        running_execution_ids: 当前正在执行的 execution_id 列表（重连恢复用）。
        sent_at: 心跳发送时间（RFC 3339 字符串）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    agent_version: str = ""
    core_api_version: str = ""
    running_execution_ids: list[str] = field(default_factory=list)
    sent_at: str = ""


class HeartbeatAck(BaseModel):
    """心跳确认（对齐 ``agent.proto`` HeartbeatAck）。

    Attributes:
        accepted: 是否接受心跳。
        control_plane_time: 控制平面处理时间（RFC 3339 字符串）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool = True
    control_plane_time: str = ""


# ---------------------------------------------------------------------- #
# 内部 Agent 追踪状态
# ---------------------------------------------------------------------- #


@dataclass
class _AgentTracker:
    """单个 Agent 的内部追踪状态。

    Attributes:
        last_heartbeat_at: 最近一次心跳的 Unix 时间戳（秒）。
        state: 当前推断的 Agent 状态。
        agent_version: 最近上报的 Agent 版本。
        core_api_version: 最近上报的核心 API 版本。
        running_execution_ids: 最近上报的在途执行列表。
    """

    last_heartbeat_at: float
    state: AgentState = AgentState.ONLINE
    agent_version: str = ""
    core_api_version: str = ""
    running_execution_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- #
# HeartbeatManager —— 控制平面侧心跳管理
# ---------------------------------------------------------------------- #


class HeartbeatManager:
    """Agent 心跳管理器（控制平面侧）。

    用 ``dict`` 内存存储心跳状态（心跳是临时状态，不需要持久化）。

    Args:
        offline_threshold_seconds: 离线阈值（秒），超过此时间未心跳则转为
            ``OFFLINE``。默认 30.0。
        degraded_threshold_seconds: 降级阈值（秒），超过此时间未心跳则转为
            ``DEGRADED``。默认 15.0。应小于 ``offline_threshold_seconds``。
    """

    def __init__(
        self,
        offline_threshold_seconds: float = 30.0,
        degraded_threshold_seconds: float = 15.0,
    ) -> None:
        if offline_threshold_seconds <= 0:
            raise ValueError("offline_threshold_seconds must be positive")
        if degraded_threshold_seconds <= 0:
            raise ValueError("degraded_threshold_seconds must be positive")
        if degraded_threshold_seconds > offline_threshold_seconds:
            raise ValueError(
                "degraded_threshold_seconds must not exceed "
                "offline_threshold_seconds"
            )
        self._offline_threshold = offline_threshold_seconds
        self._degraded_threshold = degraded_threshold_seconds
        #: agent_id → 追踪状态。
        self._agents: dict[str, _AgentTracker] = {}

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _now() -> float:
        """当前 Unix 时间戳（秒）。"""
        return time.time()

    def _compute_state(self, tracker: _AgentTracker, now: float) -> AgentState:
        """根据距上次心跳的间隔推断 Agent 状态。

        Args:
            tracker: Agent 追踪状态。
            now: 当前时间戳。

        Returns:
            推断后的 :class:`AgentState`。
        """
        elapsed = now - tracker.last_heartbeat_at
        if elapsed >= self._offline_threshold:
            return AgentState.OFFLINE
        if elapsed >= self._degraded_threshold:
            return AgentState.DEGRADED
        return AgentState.ONLINE

    # ------------------------------------------------------------------ #
    # 公开异步 API
    # ------------------------------------------------------------------ #

    async def receive(self, record: HeartbeatRecord) -> HeartbeatAck:
        """接收心跳，更新 Agent 状态为 ``ONLINE``。

        Args:
            record: 心跳记录。

        Returns:
            :class:`HeartbeatAck`，``accepted=True`` 表示已接受。
        """
        now = self._now()
        existing = self._agents.get(record.agent_id)
        if existing is not None:
            existing.last_heartbeat_at = now
            existing.state = AgentState.ONLINE
            existing.agent_version = record.agent_version
            existing.core_api_version = record.core_api_version
            existing.running_execution_ids = list(record.running_execution_ids)
        else:
            self._agents[record.agent_id] = _AgentTracker(
                last_heartbeat_at=now,
                state=AgentState.ONLINE,
                agent_version=record.agent_version,
                core_api_version=record.core_api_version,
                running_execution_ids=list(record.running_execution_ids),
            )
        return HeartbeatAck(accepted=True, control_plane_time=record.sent_at)

    async def get_agent_state(self, agent_id: str) -> AgentState:
        """查询 Agent 当前状态。

        从未心跳过的 Agent 视为 ``OFFLINE``（未知 Agent 不可用，INV-4）。

        Args:
            agent_id: Agent 标识。

        Returns:
            :class:`AgentState`。
        """
        tracker = self._agents.get(agent_id)
        if tracker is None:
            return AgentState.OFFLINE
        now = self._now()
        state = self._compute_state(tracker, now)
        tracker.state = state
        return state

    async def check_offline(self) -> list[str]:
        """检查所有 Agent，返回转为 ``OFFLINE`` 的 agent_id 列表。

        遍历所有已追踪的 Agent，将超时的 Agent 状态更新为 ``OFFLINE``，
        仅返回**本次新转为** ``OFFLINE`` 的 agent_id（之前已是 ``OFFLINE``
        的不重复返回）。

        Returns:
            本次转为 ``OFFLINE`` 的 agent_id 列表。
        """
        now = self._now()
        newly_offline: list[str] = []
        for agent_id, tracker in self._agents.items():
            new_state = self._compute_state(tracker, now)
            if (
                new_state is AgentState.OFFLINE
                and tracker.state is not AgentState.OFFLINE
            ):
                newly_offline.append(agent_id)
            tracker.state = new_state
        return newly_offline

    async def list_online_agents(self) -> list[str]:
        """列出 ``ONLINE`` 状态的 Agent。

        **失联不误判可用**（INV-4）：仅返回 ``ONLINE`` 的 Agent，
        ``DEGRADED`` / ``OFFLINE`` 均不包含，避免将失联 Agent 的资源
        回收到调度池。

        Returns:
            在线 agent_id 列表。
        """
        now = self._now()
        online: list[str] = []
        for agent_id, tracker in self._agents.items():
            state = self._compute_state(tracker, now)
            tracker.state = state
            if state is AgentState.ONLINE:
                online.append(agent_id)
        return online


__all__ = [
    "AgentState",
    "HeartbeatAck",
    "HeartbeatManager",
    "HeartbeatRecord",
]
