"""控制平面的日志输出：JSON（默认）或文本，每条记录带上当前请求的 trace-id 与关联 ID。

:func:`~forgemesh.controlplane.app.main` 在启动 uvicorn 之前调用 :func:`configure_logging`，
并以 ``log_config=None`` 启动 uvicorn，使 uvicorn 的日志（含访问日志）也经过根 logger。
配置来自环境变量，非法时拒绝启动：

* ``FORGEMESH_LOG_FORMAT``：``json``（默认）或 ``text``；
* ``FORGEMESH_LOG_LEVEL``：``DEBUG`` / ``INFO``（默认）/ ``WARNING`` / ``ERROR``，作用于根 logger。
  资源变更事件（logger ``forgemesh.events``，见
  :class:`~forgemesh.controlplane.publishers.LogPublisher`）不受它影响，总是以 INFO 写出。

JSON 格式每行一个对象：``time``（UTC，ISO 8601，毫秒）、``level``、``logger``、``message``；
在请求中记录的日志加 ``trace_id`` 与 ``correlation_id``（见
:mod:`forgemesh.controlplane.tracecontext`），带异常时加 ``exception``。文本格式为
``时间 级别 logger trace=<trace-id> corr=<关联 ID> 消息``，缺失的 ID 记为 ``-``。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from .publishers import EVENT_LOGGER_NAME
from .tracecontext import current_correlation_id, current_trace_id

LOG_FORMAT_ENV = "FORGEMESH_LOG_FORMAT"
LOG_LEVEL_ENV = "FORGEMESH_LOG_LEVEL"
LOG_FORMAT_JSON = "json"
LOG_FORMAT_TEXT = "text"
DEFAULT_LOG_FORMAT = LOG_FORMAT_JSON
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

_CONTEXT_FIELDS = ("trace_id", "correlation_id")


class LogConfigError(ValueError):
    """日志配置非法；服务应拒绝启动。"""


@dataclass(frozen=True)
class LogSettings:
    """日志配置。

    Attributes:
        format: ``json`` 或 ``text``。
        level: 根 logger 的级别。
    """

    format: str = DEFAULT_LOG_FORMAT
    level: str = DEFAULT_LOG_LEVEL


def log_settings_from_env(environ: Mapping[str, str] | None = None) -> LogSettings:
    """读取 ``FORGEMESH_LOG_FORMAT`` 与 ``FORGEMESH_LOG_LEVEL``。

    Raises:
        LogConfigError: 取值不在允许的范围内。
    """
    env = os.environ if environ is None else environ
    log_format = (env.get(LOG_FORMAT_ENV) or DEFAULT_LOG_FORMAT).strip().lower()
    if log_format not in (LOG_FORMAT_JSON, LOG_FORMAT_TEXT):
        raise LogConfigError(
            f"unsupported {LOG_FORMAT_ENV}={log_format!r}; "
            f"expected {LOG_FORMAT_JSON!r} or {LOG_FORMAT_TEXT!r}"
        )
    level = (env.get(LOG_LEVEL_ENV) or DEFAULT_LOG_LEVEL).strip().upper()
    if level not in LOG_LEVELS:
        raise LogConfigError(f"unsupported {LOG_LEVEL_ENV}={level!r}; expected one of {LOG_LEVELS}")
    return LogSettings(format=log_format, level=level)


class ContextFilter(logging.Filter):
    """为记录补上当前请求的 ``trace_id`` 与 ``correlation_id``（记录已带的取值保留）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "trace_id", None) is None:
            record.trace_id = current_trace_id()
        if getattr(record, "correlation_id", None) is None:
            record.correlation_id = current_correlation_id()
        return True


class JsonFormatter(logging.Formatter):
    """每条记录一行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, UTC)
        entry: dict[str, Any] = {
            "time": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in _CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                entry[name] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """``时间 级别 logger trace=<trace-id> corr=<关联 ID> 消息``（UTC 时间）。"""

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s %(levelname)s %(name)s trace=%(trace)s corr=%(corr)s %(message)s"
        )

    def format(self, record: logging.LogRecord) -> str:
        record.trace = getattr(record, "trace_id", None) or "-"
        record.corr = getattr(record, "correlation_id", None) or "-"
        return super().format(record)


class _ForgeMeshHandler(logging.StreamHandler):
    """:func:`configure_logging` 安装的处理器（再次配置时据此替换）。"""


def configure_logging(settings: LogSettings, stream: TextIO | None = None) -> logging.Handler:
    """在根 logger 上安装写到 ``stream``（默认标准错误）的处理器并设置级别；
    ``forgemesh.events`` 固定为 INFO。

    之前由本函数安装的处理器被替换，其他处理器不受影响。返回新安装的处理器。
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _ForgeMeshHandler)]:
        root.removeHandler(handler)
    handler = _ForgeMeshHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if settings.format == LOG_FORMAT_JSON else TextFormatter())
    root.addHandler(handler)
    root.setLevel(settings.level)
    # 事件日志是 LogPublisher 的输出，不是诊断日志：被级别过滤掉的事件同样会被标记为已发布。
    logging.getLogger(EVENT_LOGGER_NAME).setLevel(logging.INFO)
    return handler


__all__ = [
    "DEFAULT_LOG_FORMAT",
    "DEFAULT_LOG_LEVEL",
    "LOG_FORMAT_ENV",
    "LOG_FORMAT_JSON",
    "LOG_FORMAT_TEXT",
    "LOG_LEVELS",
    "LOG_LEVEL_ENV",
    "ContextFilter",
    "JsonFormatter",
    "LogConfigError",
    "LogSettings",
    "TextFormatter",
    "configure_logging",
    "log_settings_from_env",
]
