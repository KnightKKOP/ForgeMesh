"""API 错误：统一错误信封与全局异常处理。

所有错误响应都使用 ``{"error": {"code", "message", "correlationId", "traceId", "details"}}``
信封（对齐 OpenAPI ``Error`` schema），包括框架层面的 404/405、请求校验失败
（FastAPI 默认的 422 统一改为 400）以及未捕获的异常（500，不向客户端暴露内部细节）。

关联 ID 与 trace-id 取自请求头：
:class:`~forgemesh.controlplane.api.middleware.RequestContextMiddleware` 已把确定的取值写回
请求头。``500`` 处理器运行在该中间件之外（此时请求上下文已解除绑定），因此也只能从请求头读取。
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..app import error_envelope
from ..tracecontext import TRACEPARENT_HEADER, parse_traceparent

logger = logging.getLogger(__name__)

#: 承载全链路关联 ID 的请求头（HTTP 头大小写不敏感，与网关的 X-Correlation-ID 等价）。
CORRELATION_HEADER = "X-Correlation-Id"

#: 未显式指定错误码时，按 HTTP 状态码推导的默认错误码。
_DEFAULT_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    429: "RATE_LIMITED",
    503: "SERVICE_UNAVAILABLE",
}


class ApiError(StarletteHTTPException):
    """携带机器可读错误码的 HTTP 错误，由 :func:`install_exception_handlers` 渲染为错误信封。

    未安装处理器时（例如只挂载了路由的测试应用），FastAPI 仍按 ``status_code``
    响应，响应体退化为 ``{"detail": message}``。

    Args:
        status_code: HTTP 状态码。
        code: 机器可读错误码（如 ``UNAUTHENTICATED``）。
        message: 人类可读说明。
        details: 附加结构化信息，可为空。
        headers: 附加响应头（如 401 的 ``WWW-Authenticate``），可为空。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.details = details


def correlation_id_of(request: Request) -> str:
    """取请求携带的关联 ID；缺失时生成一个新的。"""
    return request.headers.get(CORRELATION_HEADER) or uuid4().hex


def trace_id_of(request: Request) -> str | None:
    """取请求 ``traceparent`` 中的 trace-id；缺失或不合法时为 ``None``。"""
    context = parse_traceparent(request.headers.get(TRACEPARENT_HEADER, ""))
    return None if context is None else context.trace_id


def validation_details(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    """提取校验错误摘要（loc/msg/type），剥离不可 JSON 序列化的 ``ctx``。"""
    return [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = getattr(exc, "code", None) or _DEFAULT_CODES.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(
            code,
            message,
            correlation_id_of(request),
            getattr(exc, "details", None),
            trace_id_of(request),
        ),
        headers=exc.headers,
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=error_envelope(
            "BAD_REQUEST",
            "request validation failed",
            correlation_id_of(request),
            {"errors": validation_details(exc)},
            trace_id_of(request),
        ),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = correlation_id_of(request)
    trace_id = trace_id_of(request)
    logger.error(
        "unhandled error: correlation_id=%s method=%s path=%s",
        correlation_id,
        request.method,
        request.url.path,
        exc_info=exc,
        extra={"correlation_id": correlation_id, "trace_id": trace_id},
    )
    return JSONResponse(
        status_code=500,
        content=error_envelope(
            "INTERNAL_ERROR", "internal server error", correlation_id, trace_id=trace_id
        ),
    )


def install_exception_handlers(app: FastAPI) -> None:
    """为应用注册全局异常处理器，使所有错误响应都符合错误信封。"""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
