"""Outbox 事件的发布器，以及按环境变量选择发布器。

应用在 lifespan 中运行 :class:`~forgemesh.controlplane.outbox.Dispatcher`，把到期的 Outbox
事件交给发布器；发布器抛出异常表示发布失败，事件按
:class:`~forgemesh.controlplane.outbox.RetryPolicy` 退避重试，失败次数达到上限后进入
``dead``。

* :class:`LogPublisher`（默认）：每个事件写一条 INFO 日志（logger ``forgemesh.events``），
  消息是事件信封的 JSON。进程没有配置 logging 时（uvicorn 只配置它自己的 logger），给这个
  logger 加一个写标准输出的 handler，每行一个信封。写日志不会失败，事件随即标记为已发布。
* :class:`WebhookPublisher`：把事件信封以 JSON ``POST`` 到 webhook；2xx 视为成功，其余状态码
  （不跟随重定向）、超时与连接错误视为失败。与 urllib 的默认行为一样，遵循 ``HTTPS_PROXY`` /
  ``HTTP_PROXY`` / ``NO_PROXY`` 等代理环境变量。

环境变量（应用启动时读取，非法时拒绝启动；空白值视同未设置）：

* ``FORGEMESH_EVENT_WEBHOOK_URL``：webhook 地址，``http`` / ``https``，必须有主机名，不得含
  用户信息与片段，只能由可打印的 ASCII 字符组成。未设置时使用 :class:`LogPublisher`。
* ``FORGEMESH_EVENT_WEBHOOK_TOKEN``：可选，以 ``Authorization: Bearer <token>`` 发送，只能由
  可见的 ASCII 字符组成。经明文 ``http`` 发往非回环地址时启动日志给出警告。
* ``FORGEMESH_EVENT_WEBHOOK_TIMEOUT_SECONDS``：连接与读取的超时（默认 ``5``，大于 0、不超过
  ``60``）。

后两个变量只能与 URL 一起设置。日志、错误信息与 ``last_error`` 只含 webhook 的 scheme、
主机与端口：路径与查询串里可能带有签名或密钥，token 一律不输出。
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import logging
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping

from .outbox import OutboxEvent
from .version import CONTROL_PLANE_VERSION

logger = logging.getLogger(__name__)

EVENT_WEBHOOK_URL_ENV = "FORGEMESH_EVENT_WEBHOOK_URL"
EVENT_WEBHOOK_TOKEN_ENV = "FORGEMESH_EVENT_WEBHOOK_TOKEN"
EVENT_WEBHOOK_TIMEOUT_ENV = "FORGEMESH_EVENT_WEBHOOK_TIMEOUT_SECONDS"
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 5.0
MAX_WEBHOOK_TIMEOUT_SECONDS = 60.0

#: :class:`LogPublisher` 默认写入的 logger。
EVENT_LOGGER_NAME = "forgemesh.events"

#: 发布器：接收一个 Outbox 事件，抛出异常表示发布失败；可以是同步函数或协程函数。
EventPublisher = Callable[[OutboxEvent], Awaitable[None] | None]


class EventConfigError(ValueError):
    """事件发布配置非法；服务应拒绝启动。"""


class PublishError(Exception):
    """webhook 发布失败；消息只含 webhook 的 origin 与失败原因。"""


class LogPublisher:
    """把事件信封写成一条 INFO 日志（单行 JSON）。

    Args:
        logger: 写入的 logger；默认 ``forgemesh.events``，进程没有配置 logging 时为它加一个
            写标准输出的 handler（见模块说明）。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger if logger is not None else _event_logger()

    def __repr__(self) -> str:
        return f"LogPublisher(logger={self._logger.name!r})"

    def __call__(self, event: OutboxEvent) -> None:
        self._logger.info(json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")))


def _event_logger() -> logging.Logger:
    events = logging.getLogger(EVENT_LOGGER_NAME)
    if not events.hasHandlers():
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        events.addHandler(handler)
        events.setLevel(logging.INFO)
    return events


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：重定向会把 ``Authorization`` 头带到另一个地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


class WebhookPublisher:
    """把事件信封以 JSON ``POST`` 到 webhook（在工作线程中执行，不阻塞事件循环）。

    请求头：``Content-Type: application/json``、``User-Agent``、``X-Forgemesh-Event-Id``
    （事件 id，接收方可据此去重）与可选的 ``Authorization: Bearer <token>``。

    Args:
        url: webhook 地址（规则见模块说明）。
        token: 可选的 Bearer token。
        timeout: 连接与读取的超时秒数。

    Raises:
        ValueError: ``url``、``token`` 或 ``timeout`` 非法；消息不含 URL 与 token。
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
    ) -> None:
        self.origin = _check_url(url, "url")
        if token is not None:
            _check_token(token, "token")
        if not _valid_timeout(timeout):
            raise ValueError(
                f"timeout must be greater than 0 and at most {MAX_WEBHOOK_TIMEOUT_SECONDS:g} "
                f"seconds, got {timeout}"
            )
        self.timeout = timeout
        self._url = url
        self._token = token
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        return f"WebhookPublisher(origin={self.origin!r}, timeout={self.timeout:g})"

    async def __call__(self, event: OutboxEvent) -> None:
        await asyncio.to_thread(self._post, event)

    def _post(self, event: OutboxEvent) -> None:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"forgemesh-control-plane/{CONTROL_PLANE_VERSION}",
            "X-Forgemesh-Event-Id": event.event_id,
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        body = json.dumps(event.payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout):
                pass
        except urllib.error.HTTPError as exc:
            exc.close()
            raise PublishError(f"webhook {self.origin} answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise PublishError(f"webhook {self.origin} failed: {_reason(exc)}") from None


def _reason(exc: BaseException) -> str:
    """失败原因：异常类型与操作系统给出的描述，不含 URL。"""
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, OSError) and exc.strerror:
        return f"{type(exc).__name__}: {exc.strerror}"
    return type(exc).__name__


def _check_url(url: str, name: str) -> str:
    """校验 webhook 地址，返回 ``scheme://host:port``。

    Raises:
        ValueError: 地址非法；消息以 ``name`` 指代地址，不含地址本身。
    """
    if not url.isascii() or any(ch.isspace() or not ch.isprintable() for ch in url):
        raise ValueError(
            f"{name} must consist of printable ASCII characters without spaces "
            "(percent-encode the path; use punycode for host names)"
        )
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        parts, port = None, 0
    if parts is None or port == 0:
        raise ValueError(f"{name} is not a valid URL (check the host and the port)")
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"{name} must use the http or https scheme")
    if not parts.hostname:
        raise ValueError(f"{name} must include a host name")
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            f"{name} must not contain user information (credentials); "
            f"send a token with {EVENT_WEBHOOK_TOKEN_ENV} instead"
        )
    if parts.fragment:
        raise ValueError(f"{name} must not contain a fragment")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    default_port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}:{port or default_port}"


def _check_token(token: str, name: str) -> None:
    if not token or not all("!" <= ch <= "~" for ch in token):
        raise ValueError(f"{name} must consist of visible ASCII characters (no spaces)")


def _valid_timeout(value: float) -> bool:
    return math.isfinite(value) and 0 < value <= MAX_WEBHOOK_TIMEOUT_SECONDS


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _get(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def _timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not _valid_timeout(value):
        raise EventConfigError(
            f"{EVENT_WEBHOOK_TIMEOUT_ENV}={raw!r} must be a number of seconds greater than 0 "
            f"and at most {MAX_WEBHOOK_TIMEOUT_SECONDS:g}"
        )
    return value


def publisher_from_env(environ: Mapping[str, str] | None = None) -> EventPublisher:
    """按环境变量构造发布器：设置了 ``FORGEMESH_EVENT_WEBHOOK_URL`` 时为
    :class:`WebhookPublisher`，否则为 :class:`LogPublisher`。

    Raises:
        EventConfigError: 取值非法，或没有设置 URL 却设置了 token / 超时；消息不含 URL 与 token。
    """
    env = os.environ if environ is None else environ
    url = _get(env, EVENT_WEBHOOK_URL_ENV)
    token = _get(env, EVENT_WEBHOOK_TOKEN_ENV)
    raw_timeout = _get(env, EVENT_WEBHOOK_TIMEOUT_ENV)
    if url is None:
        for name, value in (
            (EVENT_WEBHOOK_TOKEN_ENV, token),
            (EVENT_WEBHOOK_TIMEOUT_ENV, raw_timeout),
        ):
            if value is not None:
                raise EventConfigError(
                    f"{name} only applies to the webhook publisher; set {EVENT_WEBHOOK_URL_ENV} "
                    f"or unset {name}"
                )
        return LogPublisher()
    try:
        origin = _check_url(url, EVENT_WEBHOOK_URL_ENV)
        if token is not None:
            _check_token(token, EVENT_WEBHOOK_TOKEN_ENV)
    except ValueError as exc:
        raise EventConfigError(str(exc)) from None
    timeout = DEFAULT_WEBHOOK_TIMEOUT_SECONDS if raw_timeout is None else _timeout(raw_timeout)
    host = urllib.parse.urlsplit(url).hostname or ""
    if token is not None and origin.startswith("http://") and not _is_loopback(host):
        logger.warning(
            "%s is sent over plain http to %s and can be read on the network; use https",
            EVENT_WEBHOOK_TOKEN_ENV,
            origin,
        )
    return WebhookPublisher(url, token=token, timeout=timeout)


__all__ = [
    "DEFAULT_WEBHOOK_TIMEOUT_SECONDS",
    "EVENT_LOGGER_NAME",
    "EVENT_WEBHOOK_TIMEOUT_ENV",
    "EVENT_WEBHOOK_TOKEN_ENV",
    "EVENT_WEBHOOK_URL_ENV",
    "MAX_WEBHOOK_TIMEOUT_SECONDS",
    "EventConfigError",
    "EventPublisher",
    "LogPublisher",
    "PublishError",
    "WebhookPublisher",
    "publisher_from_env",
]
