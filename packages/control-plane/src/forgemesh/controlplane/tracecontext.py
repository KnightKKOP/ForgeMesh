"""W3C Trace Context（``traceparent``）与请求上下文。

控制平面不记录 span，也不导出追踪（OTLP 导出留到后续）：它只读取请求携带的 trace-id，
写进日志与错误信封，使一次请求在网关与控制平面的日志中可以按 trace-id 与关联 ID 对上。

* :func:`parse_traceparent` 按 W3C Trace Context 解析 ``traceparent``（只接受小写十六进制；
  trace-id / parent-id 全零、版本 ``ff`` 都不合法；版本 ``00`` 必须恰好 55 个字符，更高的
  版本按前 55 个字符解析，之后若还有内容须以 ``-`` 分隔）。不合法时调用方开始新的追踪，并
  丢弃 ``tracestate``。
* :func:`new_trace_context` 生成新的追踪上下文：随机的 trace-id 与 parent-id，flags 为
  ``00``（未采样：网关与控制平面都不记录 span）。
* :func:`request_context` 在一次请求内绑定关联 ID 与 trace-id（:mod:`contextvars`），
  :func:`current_correlation_id` / :func:`current_trace_id` 读取，供日志与错误信封使用。
  关联 ID 与 :class:`~forgemesh.controlplane.observability.CorrelationContext` 共用。
"""

from __future__ import annotations

import contextlib
import re
import secrets
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from .observability import CorrelationContext

#: 承载追踪上下文的请求头（HTTP 头大小写不敏感）。
TRACEPARENT_HEADER = "traceparent"
#: 随 ``traceparent`` 传递的厂商追踪状态；开始新的追踪时丢弃。
TRACESTATE_HEADER = "tracestate"

_TRACEPARENT = re.compile(r"([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_VERSION_00_LENGTH = 55
_INVALID_VERSION = "ff"
_INVALID_TRACE_ID = "0" * 32
_INVALID_PARENT_ID = "0" * 16
_NOT_SAMPLED = "00"

_trace_id_var: ContextVar[str | None] = ContextVar("forgemesh_trace_id", default=None)


@dataclass(frozen=True)
class TraceContext:
    """一个 ``traceparent`` 的内容。

    Attributes:
        trace_id: 32 个小写十六进制字符的 trace-id。
        parent_id: 16 个小写十六进制字符的 parent-id（调用方的 span id）。
        flags: 2 个小写十六进制字符的 trace-flags。
    """

    trace_id: str
    parent_id: str
    flags: str = _NOT_SAMPLED

    @property
    def traceparent(self) -> str:
        """以版本 ``00`` 写成的 ``traceparent``。"""
        return f"00-{self.trace_id}-{self.parent_id}-{self.flags}"


def parse_traceparent(value: str) -> TraceContext | None:
    """解析 ``traceparent``；不合法时返回 ``None``。"""
    match = _TRACEPARENT.match(value)
    if match is None:
        return None
    version, trace_id, parent_id, flags = match.groups()
    if version == "00":
        well_formed = len(value) == _VERSION_00_LENGTH
    else:
        well_formed = version != _INVALID_VERSION and (
            len(value) == _VERSION_00_LENGTH or value[_VERSION_00_LENGTH] == "-"
        )
    if not well_formed or trace_id == _INVALID_TRACE_ID or parent_id == _INVALID_PARENT_ID:
        return None
    return TraceContext(trace_id, parent_id, flags)


def _random_hex(nbytes: int) -> str:
    while True:
        value = secrets.token_hex(nbytes)
        if value.strip("0"):
            return value


def new_trace_context() -> TraceContext:
    """生成新的追踪上下文（未采样）。"""
    return TraceContext(_random_hex(16), _random_hex(8))


def current_trace_id() -> str | None:
    """当前请求的 trace-id；不在请求中时为 ``None``。"""
    return _trace_id_var.get()


def current_correlation_id() -> str | None:
    """当前请求的关联 ID；不在请求中时为 ``None``。"""
    return CorrelationContext.get_correlation_id()


@contextlib.contextmanager
def request_context(correlation_id: str, trace_id: str) -> Iterator[None]:
    """在当前上下文中绑定关联 ID 与 trace-id，退出时恢复。"""
    tokens = CorrelationContext.set(correlation_id=correlation_id)
    trace_token = _trace_id_var.set(trace_id)
    try:
        yield
    finally:
        _trace_id_var.reset(trace_token)
        CorrelationContext.reset(tokens)


__all__ = [
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
    "TraceContext",
    "current_correlation_id",
    "current_trace_id",
    "new_trace_context",
    "parse_traceparent",
    "request_context",
]
