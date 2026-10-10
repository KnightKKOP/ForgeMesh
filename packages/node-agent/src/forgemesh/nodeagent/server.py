"""Agent 注册与身份认证（WP-3.1）。

控制平面侧 :class:`AgentServer` 负责校验 Node Agent 注册请求：

* ``agent_id`` 非空；
* ``core_api_version`` 在服务端支持的版本列表内（能力协商/兼容性）；
* ``supported_task_types`` 非空（节点必须声明可执行的任务类型）。

校验通过后签发 ``session_id`` 并协商心跳间隔；否则返回 ``accepted=False`` 及原因。

节点侧 :class:`AgentClient` 封装注册握手，返回 ``session_id`` 或 ``None``。

:class:`AgentServer` 同时作为控制平面的**内存桩**，提供心跳接收、快照接收与
执行状态查询，供 :mod:`probe` / :mod:`health` / :mod:`recover` 复用，避免在
单元测试中引入真实网络依赖。

对齐 architecture 10.1（Node Agent 受控注册）与 ADR-009（最小权限/类型化任务）。
兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegisterResult:
    """注册结果。

    Attributes:
        accepted: 是否接受注册。
        session_id: 接受时签发的会话 id；拒绝时为 ``None``。
        heartbeat_interval: 协商的心跳间隔（秒）；拒绝时为 ``0``。
        reason: 拒绝原因；接受时为 ``None``。
    """

    accepted: bool
    session_id: str | None = None
    heartbeat_interval: int = 0
    reason: str | None = None


class AgentServer:
    """Agent 注册服务端（控制平面侧，内存桩）。

    Attributes:
        supported_api_versions: 接受的 ``core_api_version`` 列表，默认 ``["v1"]``。
        heartbeat_interval: 默认协商心跳间隔（秒）。
    """

    def __init__(
        self,
        *,
        supported_api_versions: list[str] | None = None,
        heartbeat_interval: int = 30,
    ) -> None:
        self.supported_api_versions = list(supported_api_versions or ["v1"])
        self.heartbeat_interval = heartbeat_interval
        # agent_id -> session_id
        self._sessions: dict[str, str] = {}
        # execution_id -> state（succeeded/failed/cancelled/running/...）
        self._executions: dict[str, str] = {}
        # agent_id -> snapshot
        self._snapshots: dict[str, dict[str, Any]] = {}
        # agent_id -> 最近一次心跳载荷
        self._heartbeats: dict[str, dict[str, Any]] = {}

    async def register(
        self,
        *,
        agent_id: str,
        agent_version: str,
        capabilities: list[str],
        supported_task_types: list[str],
        core_api_version: str,
    ) -> RegisterResult:
        """校验并注册 Agent，返回 :class:`RegisterResult`。

        校验顺序：agent_id 非空 → core_api_version 兼容 → supported_task_types 非空。
        任一不满足即拒绝（``accepted=False``），不签发 session。
        """
        if not agent_id:
            return RegisterResult(accepted=False, reason="agent_id must not be empty")
        if core_api_version not in self.supported_api_versions:
            return RegisterResult(
                accepted=False,
                reason=f"unsupported core_api_version: {core_api_version}",
            )
        if not supported_task_types:
            return RegisterResult(
                accepted=False,
                reason="supported_task_types must not be empty",
            )
        session_id = uuid.uuid4().hex
        self._sessions[agent_id] = session_id
        return RegisterResult(
            accepted=True,
            session_id=session_id,
            heartbeat_interval=self.heartbeat_interval,
        )

    async def receive_heartbeat(
        self,
        *,
        agent_id: str,
        agent_version: str,
        running_executions: list[str],
    ) -> bool:
        """接收心跳；未注册 agent 返回 ``False``。"""
        if agent_id not in self._sessions:
            return False
        self._heartbeats[agent_id] = {
            "agent_version": agent_version,
            "running_executions": list(running_executions),
        }
        return True

    async def receive_snapshot(
        self,
        *,
        agent_id: str,
        snapshot: dict[str, Any],
    ) -> bool:
        """接收资源快照；未注册 agent 返回 ``False``。"""
        if agent_id not in self._sessions:
            return False
        self._snapshots[agent_id] = dict(snapshot)
        return True

    async def get_execution_state(self, execution_id: str) -> str | None:
        """查询执行真实状态；未知返回 ``None``。"""
        return self._executions.get(execution_id)

    def set_execution_state(self, execution_id: str, state: str) -> None:
        """设置执行状态（控制平面/测试注入用）。"""
        self._executions[execution_id] = state

    @property
    def sessions(self) -> dict[str, str]:
        """已注册会话（agent_id -> session_id），只读视图。"""
        return dict(self._sessions)


class AgentClient:
    """Agent 客户端（节点侧）。

    Attributes:
        core_api_version: 本节点实现的控制平面 API 版本，注册时上报用于兼容性协商。
    """

    def __init__(self, *, core_api_version: str = "v1") -> None:
        self.core_api_version = core_api_version

    async def connect(
        self,
        server: AgentServer,
        *,
        agent_id: str,
        agent_version: str,
        capabilities: list[str],
        supported_task_types: list[str],
    ) -> str | None:
        """向 ``server`` 发起注册，成功返回 ``session_id``，失败返回 ``None``。"""
        result = await server.register(
            agent_id=agent_id,
            agent_version=agent_version,
            capabilities=capabilities,
            supported_task_types=supported_task_types,
            core_api_version=self.core_api_version,
        )
        return result.session_id if result.accepted else None


__all__ = [
    "RegisterResult",
    "AgentServer",
    "AgentClient",
]
