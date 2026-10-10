"""``forgemesh.nodeagent.server`` 单元测试。

覆盖 Agent 注册与身份认证核心语义：

* 注册成功（签发 session_id、协商心跳间隔）；
* 注册失败：空 agent_id / 不兼容 core_api_version / 空 supported_task_types；
* AgentClient.connect 成功返回 session_id、失败返回 None。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.nodeagent.server import AgentClient, AgentServer, RegisterResult

pytestmark = pytest.mark.unit


def _register_kwargs(**overrides: object) -> dict:
    """构造一份合法的注册参数，``overrides`` 可覆盖任意字段。"""
    base: dict = {
        "agent_id": "agent-1",
        "agent_version": "0.1.0",
        "capabilities": ["nvidia-gpu"],
        "supported_task_types": ["PROBE_GPU", "DEPLOY_INFERENCE_SERVICE"],
        "core_api_version": "v1",
    }
    base.update(overrides)
    return base


def test_register_success() -> None:
    """合法注册应被接受，签发 session_id 并协商心跳间隔。"""
    server = AgentServer(heartbeat_interval=15)

    result = asyncio.run(server.register(**_register_kwargs()))

    assert result.accepted is True
    assert result.session_id is not None and len(result.session_id) > 0
    assert result.heartbeat_interval == 15
    assert result.reason is None
    assert "agent-1" in server.sessions


def test_register_reject_empty_agent_id() -> None:
    """空 agent_id 应被拒绝。"""
    server = AgentServer()

    result = asyncio.run(server.register(**_register_kwargs(agent_id="")))

    assert result.accepted is False
    assert result.session_id is None
    assert result.reason is not None and "agent_id" in result.reason


def test_register_reject_incompatible_version() -> None:
    """不兼容的 core_api_version 应被拒绝。"""
    server = AgentServer(supported_api_versions=["v1"])

    result = asyncio.run(server.register(**_register_kwargs(core_api_version="v2")))

    assert result.accepted is False
    assert result.session_id is None
    assert result.reason is not None and "v2" in result.reason


def test_register_reject_empty_task_types() -> None:
    """空 supported_task_types 应被拒绝。"""
    server = AgentServer()

    result = asyncio.run(server.register(**_register_kwargs(supported_task_types=[])))

    assert result.accepted is False
    assert result.session_id is None
    assert result.reason is not None and "supported_task_types" in result.reason


def test_register_result_is_frozen() -> None:
    """RegisterResult 为 frozen dataclass，不可变。"""
    r = RegisterResult(accepted=True, session_id="s", heartbeat_interval=10)
    with pytest.raises(AttributeError):
        r.accepted = False  # type: ignore[misc]


def test_client_connect_returns_session_id() -> None:
    """connect 成功应返回 session_id。"""
    server = AgentServer()
    client = AgentClient(core_api_version="v1")

    session_id = asyncio.run(
        client.connect(
            server,
            agent_id="agent-2",
            agent_version="0.1.0",
            capabilities=["nvidia-gpu"],
            supported_task_types=["PROBE_GPU"],
        )
    )

    assert session_id is not None and len(session_id) > 0


def test_client_connect_returns_none_on_failure() -> None:
    """注册失败时 connect 应返回 None。"""
    server = AgentServer(supported_api_versions=["v1"])
    client = AgentClient(core_api_version="v9")  # 不兼容版本

    session_id = asyncio.run(
        client.connect(
            server,
            agent_id="agent-3",
            agent_version="0.1.0",
            capabilities=["nvidia-gpu"],
            supported_task_types=["PROBE_GPU"],
        )
    )

    assert session_id is None
