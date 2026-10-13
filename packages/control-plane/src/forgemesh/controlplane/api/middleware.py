"""请求遥测中间件（纯 ASGI）：记录每个 HTTP 请求的指标。

位于 Starlette 的 ``ServerErrorMiddleware`` 之内、路由之外：未捕获的异常穿过本中间件时
按 ``500`` 记录（随后由 ``ServerErrorMiddleware`` 返回 ``500`` 错误信封）。路由模板取自
路由器写入 scope 的 ``route``（405 时为路径匹配的路由），没有匹配路由时为空。
"""

from __future__ import annotations

import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..telemetry import HTTPMetrics


class TelemetryMiddleware:
    """记录 HTTP 请求的计数与耗时（见 :class:`~forgemesh.controlplane.telemetry.HTTPMetrics`）。"""

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


__all__ = ["TelemetryMiddleware"]
