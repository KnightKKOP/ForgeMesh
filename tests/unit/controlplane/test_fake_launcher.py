"""假启动器测试：每个实例是进程内的 OpenAI 兼容桩服务。

* 实例在临时端口上提供 ``GET /health`` 与 ``POST /v1/chat/completions``（SSE 流式与一次性
  响应），非法请求得到 OpenAI 风格的错误；
* 同一实例幂等键返回同一实例；监听地址与写进实例地址的主机分开配置；
* ``stop`` 释放端口并中断进行中的请求，``aclose`` 停止全部实例；不开线程，关闭后不留下
  任务；不认识的实例立即验证失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from typing import Any
from urllib.parse import urlsplit

import pytest
from forgemesh.controlplane.deployment import (
    FakeLauncher,
    LaunchedInstance,
    LaunchRequest,
    deployment_run_key,
)
from forgemesh.controlplane.deployment import fake as fake_module
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import (
    AcceleratorSpec,
    EndpointProtocol,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

CHAT_PATH = "/v1/chat/completions"
CHAT = {"model": "smoke", "messages": [{"role": "user", "content": "hi"}], "stream": True}


def _request(uid: str = "uid-1") -> LaunchRequest:
    return LaunchRequest(
        instance_key=deployment_run_key(uid),
        tenant_id="tenant-1",
        resource=ResourceRef(kind="InferenceService", namespace="team-a", name="svc", uid=uid),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        allocation_id="alloc-1",
        devices=(AllocatedDevice(node_id="node-a", device_id="gpu-0"),),
    )


def _port(instance: LaunchedInstance) -> int:
    port = urlsplit(instance.url).port
    assert port is not None
    return port


def _post(path: str, payload: Any) -> bytes:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    head = (
        f"POST {path} HTTP/1.1\r\nHost: instance\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    return head.encode() + body


async def _http(port: int, request: bytes) -> tuple[int, dict[str, str], bytes]:
    """发送一个请求并读到连接关闭，返回 ``(状态码, 小写头名的响应头, 响应体)``。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        raw = await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.lower()] = value.strip()
    return int(status_line.split()[1]), headers, body


def _events(body: bytes) -> list[str]:
    """SSE 响应体中每个事件的 ``data``。"""
    events = [event for event in body.decode().split("\n\n") if event]
    assert all(event.startswith("data: ") for event in events)
    return [event.removeprefix("data: ") for event in events]


async def _assert_refused(port: int) -> None:
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


# ---------------------------------------------------------------------- #
# 桩服务
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_an_instance_serves_health_and_streams_chat_completions() -> None:
    launcher = FakeLauncher()

    async def scenario() -> tuple[LaunchedInstance, int, Any, Any]:
        instance = await launcher.launch(_request())
        await launcher.verify(instance)
        health = await _http(_port(instance), b"GET /health HTTP/1.1\r\nHost: instance\r\n\r\n")
        chat = await _http(_port(instance), _post(f"{CHAT_PATH}?trace=1", CHAT))
        await launcher.aclose()
        return instance, _port(instance), health, chat

    instance, port, health, (status, headers, body) = asyncio.run(scenario())

    assert instance.url == f"http://127.0.0.1:{port}"
    assert instance.instance_id.startswith("fake-")
    assert instance.protocol is EndpointProtocol.OPENAI_CHAT
    assert health[0] == 200
    assert (status, headers["content-type"]) == (200, "text/event-stream")
    events = _events(body)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert {(chunk["object"], chunk["model"]) for chunk in chunks} == {
        ("chat.completion.chunk", "smoke")
    }
    assert len({chunk["id"] for chunk in chunks}) == 1
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    assert deltas[0]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert "".join(delta.get("content", "") for delta in deltas) == (
        f"Hello from the ForgeMesh fake instance {instance.instance_id}."
    )


@pytest.mark.unit
def test_a_request_without_stream_gets_one_chat_completion() -> None:
    launcher = FakeLauncher()

    async def scenario() -> tuple[LaunchedInstance, Any]:
        instance = await launcher.launch(_request())
        response = await _http(_port(instance), _post(CHAT_PATH, {"messages": CHAT["messages"]}))
        await launcher.aclose()
        return instance, response

    instance, (status, headers, body) = asyncio.run(scenario())

    completion = json.loads(body)
    assert (status, headers["content-type"]) == (200, "application/json")
    assert (completion["object"], completion["model"]) == ("chat.completion", "models/llama")
    assert completion["choices"][0]["message"] == {
        "role": "assistant",
        "content": f"Hello from the ForgeMesh fake instance {instance.instance_id}.",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("request_bytes", "status", "allow"),
    [
        (_post(CHAT_PATH, b"{not json"), 400, None),
        (_post(CHAT_PATH, [1]), 400, None),
        (_post(CHAT_PATH, {"messages": []}), 400, None),
        (_post(CHAT_PATH, {"messages": "hi"}), 400, None),
        (_post(CHAT_PATH, {**CHAT, "stream": "yes"}), 400, None),
        (b"GET /v1/chat/completions HTTP/1.1\r\nHost: instance\r\n\r\n", 405, "POST"),
        (b"POST /health HTTP/1.1\r\nHost: instance\r\nContent-Length: 0\r\n\r\n", 405, "GET"),
        (b"GET /v1/models HTTP/1.1\r\nHost: instance\r\n\r\n", 404, None),
        (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: instance\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            411,
            None,
        ),
        (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: instance\r\n"
            b"Content-Length: 2097152\r\n\r\n",
            413,
            None,
        ),
        (b"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: x\r\n\r\n", 400, None),
        (b"HELLO\r\n\r\n", 400, None),
    ],
    ids=[
        "invalid-json",
        "not-an-object",
        "no-messages",
        "messages-not-an-array",
        "stream-not-a-boolean",
        "get-chat",
        "post-health",
        "unknown-path",
        "chunked-body",
        "body-too-large",
        "invalid-length",
        "malformed-request-line",
    ],
)
def test_invalid_requests_get_an_openai_style_error(
    request_bytes: bytes, status: int, allow: str | None
) -> None:
    launcher = FakeLauncher()

    async def scenario() -> tuple[int, dict[str, str], bytes]:
        instance = await launcher.launch(_request())
        response = await _http(_port(instance), request_bytes)
        await launcher.aclose()
        return response

    actual, headers, body = asyncio.run(scenario())

    assert actual == status
    assert headers.get("allow") == allow
    assert json.loads(body)["error"]["type"] == "invalid_request_error"


@pytest.mark.unit
def test_a_client_expecting_100_continue_is_told_to_send_the_body() -> None:
    launcher = FakeLauncher()
    body = json.dumps(CHAT).encode()

    async def scenario() -> bytes:
        instance = await launcher.launch(_request())
        reader, writer = await asyncio.open_connection("127.0.0.1", _port(instance))
        writer.write(
            f"POST {CHAT_PATH} HTTP/1.1\r\nHost: instance\r\nExpect: 100-continue\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        interim = await reader.readuntil(b"\r\n\r\n")
        writer.write(body)
        rest = await reader.read()
        writer.close()
        await writer.wait_closed()
        await launcher.aclose()
        return interim + rest

    response = asyncio.run(scenario())

    assert response.startswith(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n")
    assert response.endswith(b"data: [DONE]\n\n")


# ---------------------------------------------------------------------- #
# 启动器
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_launch_returns_the_running_instance_of_a_key_and_new_ones_otherwise() -> None:
    launcher = FakeLauncher()

    async def scenario() -> dict[str, LaunchedInstance]:
        first = await launcher.launch(_request())
        again = await launcher.launch(_request())
        other = await launcher.launch(_request("uid-2"))
        await launcher.stop(first.instance_id)
        relaunched = await launcher.launch(_request())
        running = {
            name: await launcher.is_running(instance.instance_id)
            for name, instance in {"first": first, "other": other, "new": relaunched}.items()
        }
        assert running == {"first": False, "other": True, "new": True}
        await launcher.aclose()
        return {"first": first, "again": again, "other": other, "relaunched": relaunched}

    instances = asyncio.run(scenario())

    assert instances["again"] == instances["first"]
    assert len({instance.instance_id for instance in instances.values()}) == 3
    assert len({_port(instance) for instance in instances.values()}) == 3


@pytest.mark.unit
def test_instances_can_listen_on_all_addresses_and_advertise_another_host() -> None:
    launcher = FakeLauncher(bind_host="0.0.0.0", advertise_host="control-plane")

    async def scenario() -> LaunchedInstance:
        instance = await launcher.launch(_request())
        await launcher.verify(instance)
        await launcher.aclose()
        return instance

    instance = asyncio.run(scenario())

    assert (launcher.bind_host, launcher.advertise_host) == ("0.0.0.0", "control-plane")
    assert instance.url == f"http://control-plane:{_port(instance)}"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bind_host", "advertise_host", "message"),
    [
        ("localhost", None, "is not an IP address"),
        ("0.0.0.0", None, "an advertise host is required"),
        ("127.0.0.1", "0.0.0.0", "wildcard"),
        ("127.0.0.1", "not a host", "neither an IP address nor a host name"),
    ],
)
def test_unusable_addresses_are_rejected(
    bind_host: str, advertise_host: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        FakeLauncher(bind_host=bind_host, advertise_host=advertise_host)


@pytest.mark.unit
def test_stop_releases_the_port_and_interrupts_requests_in_flight() -> None:
    launcher = FakeLauncher()

    async def scenario() -> None:
        instance = await launcher.launch(_request())
        reader, writer = await asyncio.open_connection("127.0.0.1", _port(instance))
        writer.write(f"POST {CHAT_PATH} HTTP/1.1\r\n".encode())
        await writer.drain()
        await launcher.verify(instance)

        await launcher.stop(instance.instance_id)

        with contextlib.suppress(ConnectionResetError):
            assert await reader.read() == b""
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()
        await _assert_refused(_port(instance))
        assert not await launcher.is_running(instance.instance_id)
        with pytest.raises(RuntimeError, match=f"instance {instance.instance_id} is not running"):
            await launcher.verify(instance)
        await launcher.stop(instance.instance_id)
        await launcher.stop("fake-unknown")
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


@pytest.mark.unit
def test_aclose_stops_every_instance_without_threads_or_leftover_tasks() -> None:
    launcher = FakeLauncher()
    threads = set(threading.enumerate())

    async def scenario() -> None:
        instances = [await launcher.launch(_request(uid)) for uid in ("uid-1", "uid-2")]
        assert set(threading.enumerate()) == threads

        await launcher.aclose()

        for instance in instances:
            await _assert_refused(_port(instance))
            assert not await launcher.is_running(instance.instance_id)
        with pytest.raises(RuntimeError, match="the fake launcher is closed"):
            await launcher.launch(_request())
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())
    assert set(threading.enumerate()) == threads


@pytest.mark.unit
def test_verify_fails_for_unknown_instances_and_unhealthy_ones(monkeypatch) -> None:
    """重启之前的实例（启动器不认识）立即失败；``/health`` 不应答时同样失败。"""
    launcher = FakeLauncher()
    lost = LaunchedInstance(
        instance_id="fake-lost", url="http://127.0.0.1:9", protocol="openai-chat"
    )

    async def unhealthy(host: str, port: int, *, timeout: float = 5.0) -> bool:
        return False

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="instance fake-lost is not running"):
            await launcher.verify(lost)
        instance = await launcher.launch(_request())
        monkeypatch.setattr(fake_module, "probe_health", unhealthy)
        with pytest.raises(RuntimeError, match="does not answer GET /health"):
            await launcher.verify(instance)
        await launcher.aclose()

    asyncio.run(scenario())
