"""假推理运行时：每个实例是控制平面进程内的一个 OpenAI 兼容 HTTP 桩服务。

供 CI 与本地开发使用（不需要 GPU，不加载模型）。:class:`FakeLauncher` 为每个实例在一个
临时端口上启动一个 asyncio HTTP 服务（只用标准库，运行在控制平面的事件循环中，不开
线程）：

* ``GET /health`` → ``200``；
* ``POST /v1/chat/completions`` → 请求体须是带非空 ``messages`` 数组的 JSON 对象；
  ``"stream": true`` 时以 SSE 返回若干 ``chat.completion.chunk`` 事件（每个一行
  ``data: {...}``），以 ``data: [DONE]`` 结束，否则返回一个 ``chat.completion`` 对象。
  回复内容固定，含实例 id。

每个连接只处理一个请求（``Connection: close``）；请求体须以 ``Content-Length`` 给出长度，
至多 1 MiB。

实例只存在于控制平面进程中，重启之后不复存在（重启之前 ``Ready`` 的服务随之转为
``Degraded``，见 :mod:`.recovery`）。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

from forgemesh.resource.inference import EndpointProtocol

from .launcher import LaunchedInstance, LaunchRequest, RuntimeLauncher
from .network import instance_url, local_address, probe_health, resolve_hosts, socket_family

logger = logging.getLogger(__name__)

#: 默认监听地址（同时也是默认写进实例地址的主机）。
DEFAULT_BIND_HOST = "127.0.0.1"
#: 读取一个请求（请求头与请求体）的时限（秒）。
REQUEST_TIMEOUT_SECONDS = 30.0
#: 请求体的上限（字节）。
MAX_BODY_BYTES = 1024 * 1024

_HEALTH_PATH = "/health"
_CHAT_PATH = "/v1/chat/completions"


class _HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str, *, allow: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.allow = allow


@dataclass(eq=False)
class _Instance:
    key: str
    instance: LaunchedInstance
    model: str
    server: asyncio.Server
    port: int
    connections: set[asyncio.Task[None]] = field(default_factory=set)


def _response(
    status: HTTPStatus, body: bytes, content_type: str, headers: dict[str, str] | None = None
) -> bytes:
    lines = [
        f"HTTP/1.1 {status.value} {status.phrase}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        *(f"{name}: {value}" for name, value in (headers or {}).items()),
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("latin-1") + body


def _error_response(error: _HttpError) -> bytes:
    body = {"error": {"message": str(error), "type": "invalid_request_error", "code": None}}
    headers = {"Allow": error.allow} if error.allow else None
    return _response(error.status, json.dumps(body).encode(), "application/json", headers)


def _event(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


def _parse_head(head: bytes) -> tuple[str, str, dict[str, str]]:
    """解析请求行与请求头，返回 ``(method, path, headers)``（头名小写，路径不含查询串）。"""
    request_line, *header_lines = head.decode("latin-1").split("\r\n")
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _HttpError(HTTPStatus.BAD_REQUEST, "malformed request line")
    method, target, _ = parts
    headers: dict[str, str] = {}
    for line in header_lines:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or not name.strip():
            raise _HttpError(HTTPStatus.BAD_REQUEST, "malformed header line")
        headers[name.strip().lower()] = value.strip()
    return method, target.split("?", 1)[0], headers


async def _read_body(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: dict[str, str]
) -> bytes:
    if "transfer-encoding" in headers:
        raise _HttpError(HTTPStatus.LENGTH_REQUIRED, "send the request body with Content-Length")
    length = headers.get("content-length")
    if length is None:
        return b""
    if not (length.isascii() and length.isdigit()):
        raise _HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
    size = int(length)
    if size > MAX_BODY_BYTES:
        raise _HttpError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"the request body exceeds {MAX_BODY_BYTES} bytes"
        )
    if size and headers.get("expect", "").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
    return await reader.readexactly(size)


class FakeLauncher(RuntimeLauncher):
    """为每个实例启动一个 OpenAI 兼容桩服务的启动器（见模块说明）。

    Args:
        bind_host: 实例监听的 IP 地址，默认 ``127.0.0.1``；``0.0.0.0`` / ``::`` 监听全部
            地址。
        advertise_host: 写进实例地址的主机名或 IP 地址，默认同 ``bind_host``；监听全部
            地址时必须给出（如 compose 中网关可以解析的服务名 ``control-plane``）。

    Raises:
        ValueError: 地址非法，或监听全部地址却没有给出 ``advertise_host``。
    """

    def __init__(
        self, bind_host: str = DEFAULT_BIND_HOST, advertise_host: str | None = None
    ) -> None:
        self._bind_host, self._advertise_host = resolve_hosts(bind_host, advertise_host)
        self._probe_host = local_address(self._bind_host)
        self._by_key: dict[str, _Instance] = {}
        self._by_id: dict[str, _Instance] = {}
        self._closed = False

    @property
    def bind_host(self) -> str:
        """实例监听的地址。"""
        return self._bind_host

    @property
    def advertise_host(self) -> str:
        """写进实例地址的主机。"""
        return self._advertise_host

    async def launch(self, request: LaunchRequest) -> LaunchedInstance:
        """返回 ``instance_key`` 对应的运行中实例，没有时在新的临时端口上启动一个。

        Raises:
            RuntimeError: 启动器已关闭。
            OSError: 无法监听。
        """
        if self._closed:
            raise RuntimeError("the fake launcher is closed")
        existing = self._by_key.get(request.instance_key)
        if existing is not None:
            return existing.instance
        instance_id = f"fake-{uuid.uuid4().hex[:12]}"
        sock = socket.create_server((self._bind_host, 0), family=socket_family(self._bind_host))
        try:
            server = await asyncio.start_server(
                functools.partial(self._serve, instance_id), sock=sock, start_serving=False
            )
        except BaseException:
            sock.close()
            raise
        port = sock.getsockname()[1]
        state = _Instance(
            key=request.instance_key,
            instance=LaunchedInstance(
                instance_id=instance_id,
                url=instance_url(self._advertise_host, port),
                protocol=EndpointProtocol.OPENAI_CHAT,
            ),
            model=request.spec.model.reference,
            server=server,
            port=port,
        )
        self._by_key[state.key] = state
        self._by_id[instance_id] = state
        try:
            await server.start_serving()
        except BaseException:
            self._forget(state)
            server.close()
            raise
        logger.info(
            "fake instance started: instance_id=%s url=%s listen=%s:%d instance_key=%s",
            instance_id,
            state.instance.url,
            self._bind_host,
            port,
            state.key,
        )
        return state.instance

    async def verify(self, instance: LaunchedInstance) -> None:
        """实例不在运行（如重启之前的实例）时立即抛出，否则检查它的 ``GET /health``。"""
        state = self._by_id.get(instance.instance_id)
        if state is None:
            raise RuntimeError(f"instance {instance.instance_id} is not running")
        if not await probe_health(self._probe_host, state.port):
            raise RuntimeError(f"instance {instance.instance_id} does not answer GET /health")

    async def stop(self, instance_id: str) -> None:
        """停止监听并中断实例正在处理的请求，端口随之释放。"""
        state = self._by_id.get(instance_id)
        if state is not None:
            self._forget(state)
            await self._shutdown(state)

    async def is_running(self, instance_id: str) -> bool:
        return instance_id in self._by_id

    async def aclose(self) -> None:
        """停止全部实例；之后 :meth:`launch` 抛出 :class:`RuntimeError`。"""
        self._closed = True
        states = list(self._by_id.values())
        for state in states:
            self._forget(state)
        await asyncio.gather(*(self._shutdown(state) for state in states))

    def _forget(self, state: _Instance) -> None:
        self._by_id.pop(state.instance.instance_id, None)
        if self._by_key.get(state.key) is state:
            del self._by_key[state.key]

    async def _shutdown(self, state: _Instance) -> None:
        state.server.close()
        connections = list(state.connections)
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        await state.server.wait_closed()
        logger.info("fake instance stopped: instance_id=%s", state.instance.instance_id)

    async def _serve(
        self, instance_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        state = self._by_id.get(instance_id)
        task = asyncio.current_task()
        if state is None or task is None:
            writer.transport.abort()
            return
        state.connections.add(task)
        try:
            await self._exchange(state, reader, writer)
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except Exception as exc:  # noqa: BLE001 - 客户端断开或发来无法解析的请求：只影响这个连接
            logger.debug("fake instance %s dropped a connection: %r", instance_id, exc)
            writer.transport.abort()
        else:
            writer.close()
        finally:
            state.connections.discard(task)

    async def _exchange(
        self, state: _Instance, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                method, path, headers = _parse_head(await reader.readuntil(b"\r\n\r\n"))
                body = await _read_body(reader, writer, headers)
            parts = self._handle(state, method, path, body)
        except _HttpError as error:
            parts = [_error_response(error)]
        for part in parts:
            writer.write(part)
            await writer.drain()

    def _handle(self, state: _Instance, method: str, path: str, body: bytes) -> list[bytes]:
        """按路由生成响应：完整响应只有一段，流式响应是响应头加上每个事件。"""
        if path == _HEALTH_PATH:
            if method != "GET":
                raise _HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "use GET", allow="GET")
            return [_response(HTTPStatus.OK, b"", "text/plain")]
        if path == _CHAT_PATH:
            if method != "POST":
                raise _HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "use POST", allow="POST")
            return self._chat(state, body)
        raise _HttpError(HTTPStatus.NOT_FOUND, f"no route for {path}")

    def _chat(self, state: _Instance, body: bytes) -> list[bytes]:
        try:
            payload = json.loads(body)
        except ValueError:
            raise _HttpError(HTTPStatus.BAD_REQUEST, "the request body is not valid JSON") from None
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list) or not messages:
            raise _HttpError(
                HTTPStatus.BAD_REQUEST, "the request needs a non-empty 'messages' array"
            )
        stream = payload.get("stream", False)
        if not isinstance(stream, bool):
            raise _HttpError(HTTPStatus.BAD_REQUEST, "'stream' must be a boolean")
        model = payload.get("model")
        common = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "created": int(time.time()),
            "model": model if isinstance(model, str) else state.model,
        }
        text = f"Hello from the ForgeMesh fake instance {state.instance.instance_id}."
        if not stream:
            completion = {
                **common,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
            return [_response(HTTPStatus.OK, json.dumps(completion).encode(), "application/json")]
        words = text.split(" ")
        deltas: list[dict[str, Any]] = [
            {"role": "assistant", "content": ""},
            {"content": words[0]},
            *({"content": " " + word} for word in words[1:]),
        ]
        chunk = {**common, "object": "chat.completion.chunk"}
        events = [
            _event({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            for delta in deltas
        ]
        events.append(
            _event({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        )
        head = (
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n"
        )
        return [head.encode("latin-1"), *events, b"data: [DONE]\n\n"]


__all__ = ["DEFAULT_BIND_HOST", "FakeLauncher"]
