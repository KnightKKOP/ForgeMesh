"""Outbox 事件发布器测试（``forgemesh.controlplane.publishers``）。

* 配置：没有 webhook 地址时写日志；地址、token 与超时的校验；错误信息与警告不含地址的路径、
  查询串与 token；token 与超时只能与地址一起设置；
* :class:`WebhookPublisher` 对本地 HTTP 服务：请求方法、路径、请求头与信封；非 2xx（不跟随
  重定向）、超时与连接被拒绝都抛出只含 origin 的 :class:`PublishError`；
* 与 Dispatcher 配合：失败后退避重试直到送达；
* :class:`LogPublisher`：每个事件一条单行 JSON 的 INFO 日志，进程没有配置 logging 时写标准输出；
* 应用按环境变量把事件 POST 到 webhook。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from forgemesh.controlplane import app as app_module
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.auth import HeaderAuthenticator
from forgemesh.controlplane.outbox import Dispatcher, Outbox, OutboxEvent, OutboxStatus, RetryPolicy
from forgemesh.controlplane.publishers import (
    DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
    EVENT_LOGGER_NAME,
    EVENT_WEBHOOK_TIMEOUT_ENV,
    EVENT_WEBHOOK_TOKEN_ENV,
    EVENT_WEBHOOK_URL_ENV,
    EventConfigError,
    LogPublisher,
    PublishError,
    WebhookPublisher,
    publisher_from_env,
)
from forgemesh.controlplane.version import CONTROL_PLANE_VERSION
from jsonschema import Draft202012Validator

EVENT_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "api" / "schemas" / "event.schema.json").read_text(
        encoding="utf-8"
    )
)

#: 出现在 token、查询串与用户信息中的“秘密”，任何错误信息与日志都不应含有它。
SECRET = "s3cret"
TOKEN = f"tok-{SECRET}"
QUERY = f"sig={SECRET}"

HEADERS = {"X-Tenant-Id": "acme"}
BODY = {
    "kind": "InferenceService",
    "metadata": {"name": "llama", "namespace": "default"},
    "spec": {
        "model": {"reference": "models/llama"},
        "runtime": {"provider": "vllm"},
        "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}},
    },
}


@dataclass
class Received:
    method: str
    path: str
    headers: Message
    body: bytes


class Webhook:
    """本地 webhook：记录收到的请求，按 ``responses`` 依次应答（用完后一律 204）；
    ``hang`` 时不应答，直到测试结束。"""

    def __init__(self) -> None:
        self.origin = ""
        self.requests: list[Received] = []
        self.responses: list[int] = []
        self.hang = False
        self.release = threading.Event()

    def url(self) -> str:
        return f"{self.origin}/hook?{QUERY}"


def _handler(webhook: Webhook) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 约定命名
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            webhook.requests.append(Received(self.command, self.path, self.headers, body))
            if webhook.hang:
                webhook.release.wait(5)
                return
            status = webhook.responses.pop(0) if webhook.responses else 204
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return Handler


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch) -> None:
    """urllib 遵循代理环境变量；本地 webhook 不经代理。"""
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def webhook() -> Iterator[Webhook]:
    hook = Webhook()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(hook))
    hook.origin = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield hook
    finally:
        hook.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def events_logger() -> Iterator[logging.Logger]:
    """测试结束后去掉 LogPublisher 为 ``forgemesh.events`` 加的 handler，恢复级别。"""
    events = logging.getLogger(EVENT_LOGGER_NAME)
    handlers, level = list(events.handlers), events.level
    yield events
    for handler in list(events.handlers):
        if handler not in handlers:
            events.removeHandler(handler)
    events.setLevel(level)


def _event(event_id: str = "evt-1") -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        aggregate_type="InferenceService",
        aggregate_id="uid-llama",
        event_type="inference.service.created",
        payload={
            "eventId": event_id,
            "eventType": "inference.service.created",
            "data": {"name": "llama", "description": "示例模型"},
        },
    )


def _publish(publisher: WebhookPublisher, event: OutboxEvent | None = None) -> None:
    asyncio.run(publisher(event or _event()))


# ---------------------------------------------------------------------- #
# 按环境变量选择发布器
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("environ", [{}, {EVENT_WEBHOOK_URL_ENV: "  "}], ids=["unset", "blank"])
def test_without_a_webhook_url_events_are_logged(environ) -> None:
    assert isinstance(publisher_from_env(environ), LogPublisher)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "origin"),
    [
        (f"https://hooks.example.com/forgemesh?{QUERY}", "https://hooks.example.com:443"),
        ("http://127.0.0.1:9000/hook", "http://127.0.0.1:9000"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("HTTP://Hooks.Example.COM/", "http://hooks.example.com:80"),
    ],
)
def test_a_webhook_url_selects_the_webhook_publisher(url, origin) -> None:
    publisher = publisher_from_env({EVENT_WEBHOOK_URL_ENV: url, EVENT_WEBHOOK_TOKEN_ENV: TOKEN})

    assert isinstance(publisher, WebhookPublisher)
    assert publisher.origin == origin
    assert publisher.timeout == DEFAULT_WEBHOOK_TIMEOUT_SECONDS
    assert repr(publisher) == f"WebhookPublisher(origin={origin!r}, timeout=5)"


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "timeout"), [(" 2.5 ", 2.5), ("60", 60.0), ("0.01", 0.01)])
def test_the_webhook_timeout_comes_from_the_environment(raw, timeout) -> None:
    publisher = publisher_from_env(
        {EVENT_WEBHOOK_URL_ENV: "https://hooks.example.com/", EVENT_WEBHOOK_TIMEOUT_ENV: raw}
    )

    assert isinstance(publisher, WebhookPublisher)
    assert publisher.timeout == timeout


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "message"),
    [
        pytest.param(f"ftp://hooks.example.com/{SECRET}", "http or https scheme", id="ftp"),
        pytest.param(f"hooks.example.com/{SECRET}", "http or https scheme", id="no-scheme"),
        pytest.param(f"http:///{SECRET}", "must include a host name", id="no-host"),
        pytest.param(
            f"https://user:{SECRET}@hooks.example.com/",
            "must not contain user information",
            id="credentials",
        ),
        pytest.param(f"https://hooks.example.com/#{SECRET}", "must not contain a fragment", id="#"),
        pytest.param(f"http://hooks.example.com:0/{SECRET}", "not a valid URL", id="port-0"),
        pytest.param(f"http://hooks.example.com:65536/{SECRET}", "not a valid URL", id="big-port"),
        pytest.param(f"http://hooks.example.com:{SECRET}/", "not a valid URL", id="named-port"),
        pytest.param(f"http://[::1/{SECRET}", "not a valid URL", id="broken-ipv6"),
        pytest.param(f"https://hooks.example.com/a {SECRET}", "printable ASCII", id="space"),
        pytest.param(f"https://hooks.exämple.com/{SECRET}", "printable ASCII", id="non-ascii"),
    ],
)
def test_invalid_webhook_urls_are_rejected_without_echoing_them(url, message) -> None:
    with pytest.raises(EventConfigError, match=message) as excinfo:
        publisher_from_env({EVENT_WEBHOOK_URL_ENV: url})

    assert str(excinfo.value).startswith(EVENT_WEBHOOK_URL_ENV)
    assert SECRET not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "token",
    [f"tok {SECRET}", f"tök-{SECRET}", f"tok-{SECRET}\r\nX-Injected: 1"],
    ids=["space", "non-ascii", "header-injection"],
)
def test_invalid_tokens_are_rejected_without_echoing_them(token) -> None:
    with pytest.raises(EventConfigError, match="must consist of visible ASCII") as excinfo:
        publisher_from_env(
            {EVENT_WEBHOOK_URL_ENV: "https://hooks.example.com/", EVENT_WEBHOOK_TOKEN_ENV: token}
        )

    assert str(excinfo.value).startswith(EVENT_WEBHOOK_TOKEN_ENV)
    assert SECRET not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["0", "-1", "60.5", "abc", "nan", "inf", "1e400"])
def test_invalid_webhook_timeouts_are_rejected(raw) -> None:
    with pytest.raises(
        EventConfigError,
        match=f"{EVENT_WEBHOOK_TIMEOUT_ENV}=.* must be a number of seconds greater than 0 "
        "and at most 60",
    ):
        publisher_from_env(
            {EVENT_WEBHOOK_URL_ENV: "https://hooks.example.com/", EVENT_WEBHOOK_TIMEOUT_ENV: raw}
        )


@pytest.mark.unit
@pytest.mark.parametrize("name", [EVENT_WEBHOOK_TOKEN_ENV, EVENT_WEBHOOK_TIMEOUT_ENV])
def test_token_and_timeout_require_a_webhook_url(name) -> None:
    with pytest.raises(
        EventConfigError,
        match=f"{name} only applies to the webhook publisher; set {EVENT_WEBHOOK_URL_ENV}",
    ) as excinfo:
        publisher_from_env({name: SECRET})

    assert SECRET not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "token", "warned"),
    [
        (f"http://hooks.internal:8080/hook?{QUERY}", TOKEN, True),
        (f"http://hooks.internal:8080/hook?{QUERY}", None, False),
        (f"https://hooks.internal/hook?{QUERY}", TOKEN, False),
        (f"http://127.0.0.1:8080/hook?{QUERY}", TOKEN, False),
        (f"http://localhost/hook?{QUERY}", TOKEN, False),
        (f"http://[::1]:8080/hook?{QUERY}", TOKEN, False),
    ],
)
def test_a_token_sent_over_plain_http_to_another_host_is_warned_about(
    caplog, url, token, warned
) -> None:
    caplog.set_level(logging.WARNING, logger="forgemesh.controlplane.publishers")
    environ = {EVENT_WEBHOOK_URL_ENV: url}
    if token is not None:
        environ[EVENT_WEBHOOK_TOKEN_ENV] = token

    publisher_from_env(environ)

    warnings = [r.getMessage() for r in caplog.records if r.name.endswith("publishers")]
    expected = (
        f"{EVENT_WEBHOOK_TOKEN_ENV} is sent over plain http to http://hooks.internal:8080 and "
        "can be read on the network; use https"
    )
    assert warnings == ([expected] if warned else [])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "kwargs", "message"),
    [
        ("ftp://hooks.example.com/", {}, "url must use the http or https scheme"),
        ("https://hooks.example.com/", {"token": "a b"}, "token must consist of visible ASCII"),
        ("https://hooks.example.com/", {"timeout": 0}, "timeout must be greater than 0"),
        ("https://hooks.example.com/", {"timeout": float("inf")}, "at most 60 seconds"),
    ],
)
def test_the_webhook_publisher_validates_its_arguments(url, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        WebhookPublisher(url, **kwargs)


# ---------------------------------------------------------------------- #
# WebhookPublisher 对本地 HTTP 服务
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("status", [200, 202, 204])
def test_the_webhook_receives_the_envelope_as_json(webhook, status) -> None:
    webhook.responses.append(status)

    _publish(WebhookPublisher(webhook.url(), token=TOKEN))

    [request] = webhook.requests
    assert (request.method, request.path) == ("POST", f"/hook?{QUERY}")
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Forgemesh-Event-Id"] == "evt-1"
    assert request.headers["User-Agent"] == f"forgemesh-control-plane/{CONTROL_PLANE_VERSION}"
    assert json.loads(request.body.decode("utf-8")) == _event().payload


@pytest.mark.unit
def test_without_a_token_no_authorization_header_is_sent(webhook) -> None:
    _publish(WebhookPublisher(webhook.url()))

    [request] = webhook.requests
    assert request.headers["Authorization"] is None


@pytest.mark.unit
@pytest.mark.parametrize("status", [302, 307, 400, 401, 500, 503])
def test_other_answers_fail_and_redirects_are_not_followed(webhook, status) -> None:
    webhook.responses.append(status)

    with pytest.raises(PublishError) as excinfo:
        _publish(WebhookPublisher(webhook.url(), token=TOKEN))

    assert str(excinfo.value) == f"webhook {webhook.origin} answered HTTP {status}"
    assert [request.path for request in webhook.requests] == [f"/hook?{QUERY}"]


@pytest.mark.unit
def test_a_webhook_that_does_not_answer_times_out(webhook) -> None:
    webhook.hang = True
    publisher = WebhookPublisher(webhook.url(), token=TOKEN, timeout=0.2)

    with pytest.raises(PublishError) as excinfo:
        _publish(publisher)

    assert str(excinfo.value) == f"webhook {webhook.origin} failed: TimeoutError"


@pytest.mark.unit
def test_an_unreachable_webhook_fails_with_the_reason() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(PublishError) as excinfo:
        _publish(WebhookPublisher(f"http://127.0.0.1:{port}/hook?{QUERY}", token=TOKEN))

    assert str(excinfo.value).startswith(
        f"webhook http://127.0.0.1:{port} failed: ConnectionRefusedError"
    )
    assert SECRET not in str(excinfo.value)


@pytest.mark.unit
def test_the_dispatcher_retries_a_failed_webhook_until_it_is_delivered(webhook) -> None:
    """第一次应答 503：事件留在 pending，退避到期之前不再发送，到期后送达并标记已发布。"""
    webhook.responses.append(503)
    outbox = Outbox()
    with outbox.database.transaction():
        outbox.put(_event())
    now = [time.time()]
    dispatcher = Dispatcher(
        outbox, WebhookPublisher(webhook.url(), token=TOKEN), clock=lambda: now[0]
    )

    async def scenario() -> None:
        assert await dispatcher.dispatch_once() == 0
        failed = outbox.get("evt-1")
        assert failed is not None
        assert (failed.status, failed.attempts) == (OutboxStatus.PENDING, 1)
        assert failed.last_error == f"PublishError: webhook {webhook.origin} answered HTTP 503"

        assert await dispatcher.dispatch_once() == 0
        assert len(webhook.requests) == 1

        now[0] += RetryPolicy().delay(1)
        assert await dispatcher.dispatch_once() == 1
        await outbox.close()

    asyncio.run(scenario())

    assert [r.headers["X-Forgemesh-Event-Id"] for r in webhook.requests] == ["evt-1", "evt-1"]


# ---------------------------------------------------------------------- #
# LogPublisher
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_log_publisher_writes_one_json_line_per_event(caplog, events_logger) -> None:
    caplog.set_level(logging.INFO, logger=EVENT_LOGGER_NAME)
    publisher = LogPublisher()

    publisher(_event())

    [record] = [r for r in caplog.records if r.name == EVENT_LOGGER_NAME]
    assert record.levelno == logging.INFO
    assert "\n" not in record.getMessage()
    assert json.loads(record.getMessage()) == _event().payload
    assert events_logger.handlers == []
    assert repr(publisher) == f"LogPublisher(logger={EVENT_LOGGER_NAME!r})"


@pytest.mark.unit
def test_without_logging_configured_events_are_written_to_stdout(capsys, events_logger) -> None:
    """uvicorn 只配置它自己的 logger：根 logger 没有 handler 时事件仍然可见。"""
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    for handler in root_handlers:
        root.removeHandler(handler)
    try:
        publisher = LogPublisher()
    finally:
        for handler in root_handlers:
            root.addHandler(handler)

    publisher(_event("evt-1"))
    publisher(_event("evt-2"))

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["eventId"] for line in lines] == ["evt-1", "evt-2"]


# ---------------------------------------------------------------------- #
# 应用按环境变量发布到 webhook
# ---------------------------------------------------------------------- #


def _wait_for_request(webhook: Webhook, event_type: str, timeout: float = 5.0) -> Received:
    deadline = time.monotonic() + timeout
    while True:
        for request in list(webhook.requests):
            if json.loads(request.body)["eventType"] == event_type:
                return request
        assert time.monotonic() < deadline, f"the webhook did not receive {event_type}"
        time.sleep(0.02)


@pytest.mark.unit
def test_the_app_posts_resource_events_to_the_configured_webhook(
    tmp_path, monkeypatch, webhook, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="forgemesh.controlplane.app")
    monkeypatch.setattr(app_module, "EVENT_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setenv(EVENT_WEBHOOK_URL_ENV, webhook.url())
    monkeypatch.setenv(EVENT_WEBHOOK_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(EVENT_WEBHOOK_TIMEOUT_ENV, "2")
    app = create_app(db_path=str(tmp_path / "cp.db"), authenticator=HeaderAuthenticator())

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/inference-services",
            json=BODY,
            headers={**HEADERS, "X-Correlation-Id": "corr-webhook"},
        )
        assert created.status_code == 201
        request = _wait_for_request(webhook, "inference.service.created")

    envelope = json.loads(request.body)
    Draft202012Validator(EVENT_SCHEMA).validate(envelope)
    assert envelope["correlationId"] == "corr-webhook"
    assert envelope["resourceUid"] == created.json()["metadata"]["uid"]
    assert request.headers["X-Forgemesh-Event-Id"] == envelope["eventId"]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert (
        f"outbox events are published with WebhookPublisher(origin='{webhook.origin}', timeout=2)"
        in caplog.messages
    )
    assert not any(SECRET in message for message in caplog.messages)
