"""请求中间件（纯 ASGI）：请求上下文（关联 ID 与 trace-id）与请求指标。

两者都位于 Starlette 的 ``ServerErrorMiddleware`` 之内、路由之外：未捕获的异常穿过它们时，
请求指标按 ``500`` 记录，随后由 ``ServerErrorMiddleware`` 的处理器返回 ``500`` 错误信封。
"""

from __future__ import annotations

import time
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..telemetry import HTTPMetrics
from ..tracecontext import (
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    new_trace_context,
    parse_traceparent,
    request_context,
)
from .errors import CORRELATION_HEADER

_CORRELATION = CORRELATION_HEADER.lower().encode("latin-1")
_TRACEPARENT = TRACEPARENT_HEADER.encode("latin-1")
_TRACESTATE = TRACESTATE_HEADER.encode("latin-1")


class RequestContextMiddleware:
    """确定每个 HTTP 请求的关联 ID 与追踪上下文，并在处理期间绑定。

    * 关联 ID 取第一个非空的 ``X-Correlation-Id``（原样沿用），没有时生成；
    * ``traceparent`` 恰好一个且合法时沿用其 trace-id，否则开始新的追踪并丢弃 ``tracestate``；
    * 确定的取值写回 scope 的请求头（就地修改，两者各留一个）：路由读到的请求头与日志一致，
      本中间件之外的 ``ServerErrorMiddleware`` 的 ``500`` 处理器也能读到。

    响应不回写这两个头：经网关访问时由网关回写关联 ID，错误信封带有两者。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: list[tuple[bytes, bytes]] = scope["headers"]
        inbound = next((v for n, v in headers if n == _CORRELATION and v), b"")
        correlation_id = inbound.decode("latin-1") or uuid4().hex
        traceparents = [v.decode("latin-1") for n, v in headers if n == _TRACEPARENT]
        context = parse_traceparent(traceparents[0]) if len(traceparents) == 1 else None
        dropped = {_CORRELATION, _TRACEPARENT}
        if context is None:
            context = new_trace_context()
            dropped.add(_TRACESTATE)
        scope["headers"] = [
            *((n, v) for n, v in headers if n not in dropped),
            (_CORRELATION, correlation_id.encode("latin-1")),
            (_TRACEPARENT, context.traceparent.encode("latin-1")),
        ]
        with request_context(correlation_id, context.trace_id):
            await self.app(scope, receive, send)


class TelemetryMiddleware:
    """记录 HTTP 请求的计数与耗时（见 :class:`~forgemesh.controlplane.telemetry.HTTPMetrics`）。

    路由模板取自路由器写入 scope 的 ``route``（405 时为路径匹配的路由），没有匹配路由时为空。
    """

    def __init__(self, app: ASGIApp, *, metrics: HTTPMetrics) -> None:
        self.app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_status)
        finally:
            route = getattr(scope.get("route"), "path", None)
            self._metrics.observe(scope["method"], route, status, time.perf_counter() - started)


__all__ = ["RequestContextMiddleware", "TelemetryMiddleware"]
