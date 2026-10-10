"""可观测性三件套：Metrics / Logs / Traces 共享 correlation_id。

对齐 development-plan WP-4.4。提供以下能力：

* :class:`CorrelationContext` —— 关联上下文（correlation_id / tenant_id /
  request_id / operation），基于 :mod:`contextvars` 在异步间自动传播。
* :class:`MetricsRecorder` —— 指标采集器（counter / gauge / histogram），
  覆盖 8 类关键指标。
* :class:`StructuredLogger` —— 结构化日志（JSON），自动对敏感字段脱敏。
* :class:`TraceSpan` / :class:`TraceRecorder` —— 分布式追踪 span，
  correlation_id 累计贯穿所有 span，parent span 通过 contextvars 传播。

兼容 Python 3.9+（运行环境 3.12）。
"""

from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar, Token
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------- #
# 关联上下文
# ---------------------------------------------------------------------- #

_correlation_id_var: ContextVar[str | None] = ContextVar(
    "forgemesh_correlation_id", default=None
)
_tenant_id_var: ContextVar[str | None] = ContextVar(
    "forgemesh_tenant_id", default=None
)
_request_id_var: ContextVar[str | None] = ContextVar(
    "forgemesh_request_id", default=None
)
_operation_var: ContextVar[str | None] = ContextVar(
    "forgemesh_operation", default=None
)


def new_correlation_id() -> str:
    """生成唯一 correlation_id（uuid4 十六进制字符串）。"""
    return str(uuid.uuid4())


class CorrelationContext:
    """关联上下文，基于 :mod:`contextvars` 在异步间传播。

    用法::

        cid = new_correlation_id()
        token = CorrelationContext.set(correlation_id=cid, tenant_id="t1")
        try:
            # 业务逻辑 / await ...
            assert CorrelationContext.get_correlation_id() == cid
        finally:
            CorrelationContext.reset(token)

    也可作为上下文管理器使用::

        with CorrelationContext.bind(correlation_id=cid):
            ...

    Attributes:
        correlation_id: 关联 ID，全链路贯穿。
        tenant_id: 租户标识。
        request_id: 请求标识。
        operation: 操作名称。
    """

    def __init__(
        self,
        correlation_id: str | None = None,
        tenant_id: str | None = None,
        request_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.tenant_id = tenant_id
        self.request_id = request_id
        self.operation = operation

    # -- 类方法：contextvars 读写 ------------------------------------------------

    @classmethod
    def set(
        cls,
        *,
        correlation_id: str | None = None,
        tenant_id: str | None = None,
        request_id: str | None = None,
        operation: str | None = None,
    ) -> tuple[Token[str | None], ...]:
        """设置上下文变量，返回 token 元组供 :meth:`reset` 恢复。"""
        tokens: list[Token[str | None]] = []
        if correlation_id is not None:
            tokens.append(_correlation_id_var.set(correlation_id))
        if tenant_id is not None:
            tokens.append(_tenant_id_var.set(tenant_id))
        if request_id is not None:
            tokens.append(_request_id_var.set(request_id))
        if operation is not None:
            tokens.append(_operation_var.set(operation))
        return tuple(tokens)

    @classmethod
    def reset(cls, tokens: tuple[Token[str | None], ...]) -> None:
        """恢复上下文变量到设置前的状态。"""
        for token in tokens:
            # ContextVar.reset 需要知道是哪个 var，这里通过 token 的 var 属性恢复
            token.var.reset(token)

    @classmethod
    def get_correlation_id(cls) -> str | None:
        """获取当前 correlation_id。"""
        return _correlation_id_var.get()

    @classmethod
    def get_tenant_id(cls) -> str | None:
        """获取当前 tenant_id。"""
        return _tenant_id_var.get()

    @classmethod
    def get_request_id(cls) -> str | None:
        """获取当前 request_id。"""
        return _request_id_var.get()

    @classmethod
    def get_operation(cls) -> str | None:
        """获取当前 operation。"""
        return _operation_var.get()

    @classmethod
    def current(cls) -> CorrelationContext:
        """从 contextvars 读取当前上下文快照。"""
        return cls(
            correlation_id=_correlation_id_var.get(),
            tenant_id=_tenant_id_var.get(),
            request_id=_request_id_var.get(),
            operation=_operation_var.get(),
        )

    # -- 上下文管理器 -----------------------------------------------------------

    def __enter__(self) -> CorrelationContext:
        self._tokens = CorrelationContext.set(
            correlation_id=self.correlation_id,
            tenant_id=self.tenant_id,
            request_id=self.request_id,
            operation=self.operation,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        CorrelationContext.reset(self._tokens)


# ---------------------------------------------------------------------- #
# 指标采集器
# ---------------------------------------------------------------------- #

#: 8 类关键指标名称。
KEY_METRICS: frozenset[str] = frozenset(
    {
        "request_count",
        "request_latency_ms",
        "active_workflows",
        "decision_count",
        "plugin_invocation_count",
        "allocation_count",
        "quota_usage",
        "error_count",
    }
)


def _tags_key(tags: dict[str, str] | None) -> str:
    """将 tags 规范化为可哈希的字符串键。"""
    if not tags:
        return ""
    return json.dumps(tags, sort_keys=True, ensure_ascii=False)


class _MetricEntry(BaseModel):
    """单个指标条目（name + tags 唯一）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    tags: dict[str, str] = Field(default_factory=dict)
    value: float = 0.0


class _HistogramEntry(BaseModel):
    """直方图条目（name + tags 唯一，收集所有采样值）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    tags: dict[str, str] = Field(default_factory=dict)
    values: list[float] = Field(default_factory=list)


class MetricsRecorder:
    """指标采集器，支持 counter / gauge / histogram 三种类型。

    * **counter** —— 单调递增计数器，相同 name+tags 累加。
    * **gauge** —— 可增可减的仪表，相同 name+tags 覆盖。
    * **histogram** —— 直方图，相同 name+tags 收集所有采样值。
    """

    def __init__(self) -> None:
        self._counters: dict[str, _MetricEntry] = {}
        self._gauges: dict[str, _MetricEntry] = {}
        self._histograms: dict[str, _HistogramEntry] = {}

    # -- counter ---------------------------------------------------------------

    def record_counter(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        """记录计数器（累加）。"""
        key = f"{name}:{_tags_key(tags)}"
        entry = self._counters.get(key)
        if entry is None:
            entry = _MetricEntry(name=name, tags=tags or {}, value=0.0)
            self._counters[key] = entry
        entry.value += value

    # -- gauge -----------------------------------------------------------------

    def record_gauge(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        """记录仪表（覆盖）。"""
        key = f"{name}:{_tags_key(tags)}"
        self._gauges[key] = _MetricEntry(name=name, tags=tags or {}, value=value)

    # -- histogram -------------------------------------------------------------

    def record_histogram(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        """记录直方图采样值（追加）。"""
        key = f"{name}:{_tags_key(tags)}"
        entry = self._histograms.get(key)
        if entry is None:
            entry = _HistogramEntry(name=name, tags=tags or {})
            self._histograms[key] = entry
        entry.values.append(value)

    # -- 快照 ------------------------------------------------------------------

    def get_metrics(self) -> dict[str, Any]:
        """获取所有指标快照。

        Returns:
            包含 ``counters`` / ``gauges`` / ``histograms`` 三个列表的字典。
            每个条目含 ``name`` / ``tags`` / ``value``（histogram 为 ``values``）。
        """
        counters = [
            {"name": e.name, "tags": e.tags, "value": e.value}
            for e in self._counters.values()
        ]
        gauges = [
            {"name": e.name, "tags": e.tags, "value": e.value}
            for e in self._gauges.values()
        ]
        histograms = [
            {"name": e.name, "tags": e.tags, "values": list(e.values)}
            for e in self._histograms.values()
        ]
        return {
            "counters": counters,
            "gauges": gauges,
            "histograms": histograms,
        }


# ---------------------------------------------------------------------- #
# 结构化日志
# ---------------------------------------------------------------------- #

#: 敏感字段名集合（小写匹配）。
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {"password", "token", "secret", "api_key", "credentials"}
)

#: 脱敏占位符。
_REDACTED = "***"


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    """对敏感字段脱敏（递归处理嵌套字典）。"""
    redacted: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in SENSITIVE_FIELDS:
            redacted[key] = _REDACTED
        elif isinstance(value, dict):
            redacted[key] = _redact(value)
        else:
            redacted[key] = value
    return redacted


class StructuredLogger:
    """结构化日志（JSON 格式），自动对敏感字段脱敏。

    Args:
        name: 日志器名称，默认 ``forgemesh``。
        sink: 可选的日志输出回调，接收 JSON 字符串。默认仅存内存。

    Class Attributes:
        SENSITIVE_FIELDS: 敏感字段名集合，匹配的字段值替换为 ``"***"``。
    """

    SENSITIVE_FIELDS: frozenset[str] = SENSITIVE_FIELDS

    def __init__(
        self,
        name: str = "forgemesh",
        sink: Any = None,
    ) -> None:
        self._name = name
        self._sink = sink
        self._records: list[dict[str, Any]] = []

    def log(
        self,
        level: str,
        message: str,
        correlation_id: str | None = None,
        **fields: Any,
    ) -> None:
        """写入一条结构化日志（JSON 格式）。

        Args:
            level: 日志级别（info / warn / error / debug 等）。
            message: 日志消息。
            correlation_id: 关联 ID，未提供时从上下文读取。
            **fields: 附加字段，敏感字段自动脱敏。
        """
        cid = correlation_id if correlation_id is not None else _correlation_id_var.get()
        record: dict[str, Any] = {
            "timestamp": time.time(),
            "level": level,
            "logger": self._name,
            "message": message,
            "correlation_id": cid,
            **_redact(fields),
        }
        self._records.append(record)
        if self._sink is not None:
            self._sink(json.dumps(record, ensure_ascii=False, default=str))

    def info(self, message: str, correlation_id: str | None = None, **fields: Any) -> None:
        """INFO 级别日志。"""
        self.log("info", message, correlation_id, **fields)

    def warn(self, message: str, correlation_id: str | None = None, **fields: Any) -> None:
        """WARN 级别日志。"""
        self.log("warn", message, correlation_id, **fields)

    def error(self, message: str, correlation_id: str | None = None, **fields: Any) -> None:
        """ERROR 级别日志。"""
        self.log("error", message, correlation_id, **fields)

    def debug(self, message: str, correlation_id: str | None = None, **fields: Any) -> None:
        """DEBUG 级别日志。"""
        self.log("debug", message, correlation_id, **fields)

    def get_records(self) -> list[dict[str, Any]]:
        """获取所有已记录的日志条目（用于测试 / 排查）。"""
        return list(self._records)


# ---------------------------------------------------------------------- #
# 分布式追踪
# ---------------------------------------------------------------------- #

_parent_span_id_var: ContextVar[str | None] = ContextVar(
    "forgemesh_parent_span_id", default=None
)


class TraceSpan:
    """追踪 span。

    Attributes:
        span_id: span 唯一标识。
        name: span 名称。
        correlation_id: 关联 ID，贯穿同一调用链的所有 span。
        parent_id: 父 span ID，``None`` 表示根 span。
        attributes: span 属性。
    """

    def __init__(
        self,
        name: str,
        correlation_id: str,
        parent_id: str | None = None,
    ) -> None:
        self.span_id: str = str(uuid.uuid4())
        self.name: str = name
        self.correlation_id: str = correlation_id
        self.parent_id: str | None = parent_id
        self.attributes: dict[str, Any] = {}
        self._start_time: float | None = None
        self._end_time: float | None = None
        self._token: Token[str | None] | None = None

    def start(self) -> None:
        """开始 span，记录起始时间并将自身设为当前 parent。"""
        self._start_time = time.time()
        # 将当前 span 设为 parent，后续子 span 自动链接
        self._token = _parent_span_id_var.set(self.span_id)

    def end(self) -> None:
        """结束 span，记录结束时间并恢复 parent 上下文。"""
        self._end_time = time.time()
        if self._token is not None:
            _parent_span_id_var.reset(self._token)
            self._token = None

    def add_attribute(self, key: str, value: Any) -> None:
        """添加 span 属性。"""
        self.attributes[key] = value

    @property
    def duration_ms(self) -> float:
        """持续时间（毫秒）；未结束时返回已经过的时间。"""
        if self._start_time is None:
            return 0.0
        end = self._end_time if self._end_time is not None else time.time()
        return (end - self._start_time) * 1000.0

    @property
    def started(self) -> bool:
        """是否已开始。"""
        return self._start_time is not None

    @property
    def ended(self) -> bool:
        """是否已结束。"""
        return self._end_time is not None


class TraceRecorder:
    """追踪记录器，管理 span 生命周期。

    correlation_id 累计贯穿所有 span：同一 recorder 实例中所有 span
    共享调用方传入的 correlation_id，parent span 通过 contextvars 自动链接。
    """

    def __init__(self) -> None:
        self._spans: list[TraceSpan] = []

    def start_span(self, name: str, correlation_id: str) -> TraceSpan:
        """开始一个新 span。

        parent_id 自动从 contextvars 读取（若当前处于某个 span 内）。
        """
        parent_id = _parent_span_id_var.get()
        span = TraceSpan(
            name=name,
            correlation_id=correlation_id,
            parent_id=parent_id,
        )
        span.start()
        self._spans.append(span)
        return span

    def end_span(self, span: TraceSpan) -> None:
        """结束指定 span。"""
        span.end()

    def get_spans(self) -> list[TraceSpan]:
        """获取所有 span（按创建顺序）。"""
        return list(self._spans)


__all__ = [
    "CorrelationContext",
    "KEY_METRICS",
    "MetricsRecorder",
    "SENSITIVE_FIELDS",
    "StructuredLogger",
    "TraceRecorder",
    "TraceSpan",
    "new_correlation_id",
]
