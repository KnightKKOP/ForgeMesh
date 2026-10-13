"""请求上下文：中间件沿用或生成关联 ID 与 trace-id，错误信封与日志带上同一对 ID。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from forgemesh.controlplane.api.dependencies import get_registry, reset_runtime_components
from forgemesh.controlplane.api.middleware import RequestContextMiddleware
from forgemesh.controlplane.app import create_app, error_envelope
from forgemesh.controlplane.auth import HeaderAuthenticator
from forgemesh.controlplane.deployment import RUNTIME_LAUNCHER_ENV
from forgemesh.controlplane.logs import LogSettings, configure_logging
from forgemesh.controlplane.publishers import EVENT_LOGGER_NAME
from forgemesh.controlplane.scheduler import INVENTORY_FILE_ENV
from forgemesh.controlplane.tracecontext import (
    current_correlation_id,
    current_trace_id,
    parse_traceparent,
    request_context,
)
from forgemesh.core.errors import ConflictError
from jsonschema import Draft202012Validator

OPENAPI = Path(__file__).resolve().parents[3] / "api" / "openapi" / "openapi.yaml"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
VALID = f"00-{TRACE_ID}-00f067aa0ba902b7-01"
HEX32 = re.compile(r"[0-9a-f]{32}")
HEADERS = {"X-Tenant-Id": "acme"}


@pytest.fixture(autouse=True)
def _reset_components(monkeypatch):
    monkeypatch.delenv(INVENTORY_FILE_ENV, raising=False)
    monkeypatch.delenv(RUNTIME_LAUNCHER_ENV, raising=False)
    yield
    reset_runtime_components()


@pytest.fixture
def log_entries():
    root = logging.getLogger()
    events = logging.getLogger(EVENT_LOGGER_NAME)
    level, events_level = root.level, events.level
    stream = io.StringIO()
    handler = configure_logging(LogSettings(), stream)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)
    root.setLevel(level)
    events.setLevel(events_level)


async def _receive() -> dict[str, Any]:
    raise AssertionError("the request body must not be read")


async def _send(message: dict[str, Any]) -> None:
    raise AssertionError(f"unexpected message {message}")


def _through_middleware(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    """返回内层应用看到的请求头与上下文，以及请求结束后的上下文。"""
    seen: dict[str, Any] = {}

    async def inner(scope, receive, send) -> None:
        seen["headers"] = list(scope["headers"])
        seen["context"] = (current_correlation_id(), current_trace_id())

    async def request() -> None:
        scope = {"type": "http", "method": "GET", "path": "/", "headers": headers}
        await RequestContextMiddleware(inner)(scope, _receive, _send)
        seen["after"] = (current_correlation_id(), current_trace_id())

    asyncio.run(request())
    return seen


def _values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in headers if key == name]


def _dev_app(tmp_path):
    return create_app(db_path=str(tmp_path / "cp.db"), authenticator=HeaderAuthenticator())


def _error_validator() -> Draft202012Validator:
    """按 OpenAPI ``Error`` schema 校验，错误对象收紧为不允许未声明的字段。"""
    spec = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    schema = spec["components"]["schemas"]["Error"]
    schema["properties"]["error"]["additionalProperties"] = False
    return Draft202012Validator(schema)


@pytest.mark.unit
def test_valid_trace_context_and_correlation_id_are_kept() -> None:
    seen = _through_middleware(
        [
            (b"x-tenant-id", b"acme"),
            (b"traceparent", VALID.encode()),
            (b"tracestate", b"vendor=1"),
            (b"x-correlation-id", b"corr-1"),
        ]
    )

    assert seen["headers"] == [
        (b"x-tenant-id", b"acme"),
        (b"tracestate", b"vendor=1"),
        (b"x-correlation-id", b"corr-1"),
        (b"traceparent", VALID.encode()),
    ]
    assert seen["context"] == ("corr-1", TRACE_ID)
    assert seen["after"] == (None, None)


@pytest.mark.unit
def test_missing_ids_are_generated_and_tracestate_is_dropped() -> None:
    seen = _through_middleware([(b"tracestate", b"vendor=1")])

    (correlation_id,) = _values(seen["headers"], b"x-correlation-id")
    (traceparent,) = _values(seen["headers"], b"traceparent")
    context = parse_traceparent(traceparent)
    assert HEX32.fullmatch(correlation_id)
    assert context is not None
    assert context.flags == "00"
    assert _values(seen["headers"], b"tracestate") == []
    assert seen["context"] == (correlation_id, context.trace_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "traceparents",
    [[VALID.replace("4bf9", "4BF9")], [f"ff{VALID[2:]}"], [VALID, VALID]],
    ids=["uppercase", "version-ff", "repeated"],
)
def test_invalid_or_repeated_traceparent_starts_a_new_trace(traceparents: list[str]) -> None:
    headers = [(b"traceparent", value.encode()) for value in traceparents]
    seen = _through_middleware([*headers, (b"tracestate", b"vendor=1")])

    (traceparent,) = _values(seen["headers"], b"traceparent")
    context = parse_traceparent(traceparent)
    assert context is not None
    assert context.trace_id != TRACE_ID
    assert _values(seen["headers"], b"tracestate") == []
    assert seen["context"][1] == context.trace_id


@pytest.mark.unit
def test_first_non_empty_correlation_id_wins() -> None:
    seen = _through_middleware(
        [
            (b"x-correlation-id", b""),
            (b"x-correlation-id", b"corr-2"),
            (b"x-correlation-id", b"corr-3"),
        ]
    )

    assert _values(seen["headers"], b"x-correlation-id") == ["corr-2"]
    assert seen["context"][0] == "corr-2"


@pytest.mark.unit
def test_context_is_unbound_when_the_application_raises() -> None:
    async def inner(scope, receive, send) -> None:
        raise RuntimeError("boom")

    async def request() -> tuple[str | None, str | None]:
        with pytest.raises(RuntimeError):
            await RequestContextMiddleware(inner)({"type": "http", "headers": []}, _receive, _send)
        return current_correlation_id(), current_trace_id()

    assert asyncio.run(request()) == (None, None)


@pytest.mark.unit
def test_non_http_scopes_pass_through_untouched() -> None:
    seen: dict[str, Any] = {}

    async def inner(scope, receive, send) -> None:
        seen["scope"] = scope
        seen["context"] = (current_correlation_id(), current_trace_id())

    asyncio.run(RequestContextMiddleware(inner)({"type": "lifespan"}, _receive, _send))

    assert seen == {"scope": {"type": "lifespan"}, "context": (None, None)}


@pytest.mark.unit
def test_error_envelope_falls_back_to_the_request_context() -> None:
    assert error_envelope("CONFLICT", "m") == {"error": {"code": "CONFLICT", "message": "m"}}

    with request_context("corr-1", TRACE_ID):
        implicit = error_envelope("CONFLICT", "m")
        explicit = error_envelope("CONFLICT", "m", "corr-2", trace_id="b" * 32)

    assert implicit["error"] == {
        "code": "CONFLICT",
        "message": "m",
        "correlationId": "corr-1",
        "traceId": TRACE_ID,
    }
    assert explicit["error"]["correlationId"] == "corr-2"
    assert explicit["error"]["traceId"] == "b" * 32


@pytest.mark.unit
def test_error_envelopes_and_logs_carry_the_inbound_ids(tmp_path, monkeypatch, log_entries) -> None:
    async def conflicting_delete(*args, **kwargs):
        logging.getLogger("forgemesh.test").warning("delete conflicted")
        raise ConflictError("changed concurrently")

    headers = {**HEADERS, "traceparent": VALID, "X-Correlation-Id": "corr-1"}
    with TestClient(_dev_app(tmp_path)) as client:
        monkeypatch.setattr(get_registry(), "request_delete", conflicting_delete)
        responses = [
            client.get("/api/v1/inference-services/missing", headers=headers),
            client.get("/api/v1/inference-services", params={"limit": 0}, headers=headers),
            client.get("/api/v1/inference-services", params={"limit": "many"}, headers=headers),
            client.delete("/api/v1/inference-services/llama", headers=headers),
            client.put("/healthz", headers=headers),
        ]

    assert [response.status_code for response in responses] == [404, 400, 400, 409, 405]
    validator = _error_validator()
    for response in responses:
        validator.validate(response.json())
        error = response.json()["error"]
        assert (error["traceId"], error["correlationId"]) == (TRACE_ID, "corr-1")
    (logged,) = [entry for entry in log_entries() if entry["message"] == "delete conflicted"]
    assert (logged["trace_id"], logged["correlation_id"]) == (TRACE_ID, "corr-1")


@pytest.mark.unit
def test_generated_ids_match_between_logs_and_the_500_envelope(
    tmp_path, monkeypatch, log_entries
) -> None:
    async def failing_list(*args, **kwargs):
        logging.getLogger("forgemesh.test").warning("listing failed")
        raise RuntimeError("secret internal detail")

    with TestClient(_dev_app(tmp_path), raise_server_exceptions=False) as client:
        monkeypatch.setattr(get_registry(), "list_page", failing_list)
        response = client.get("/api/v1/inference-services", headers=HEADERS)

    assert response.status_code == 500
    _error_validator().validate(response.json())
    error = response.json()["error"]
    assert HEX32.fullmatch(error["traceId"])
    assert HEX32.fullmatch(error["correlationId"])
    entries = log_entries()
    (inside,) = [entry for entry in entries if entry["message"] == "listing failed"]
    (unhandled,) = [entry for entry in entries if entry["message"].startswith("unhandled error")]
    for entry in (inside, unhandled):
        assert (entry["trace_id"], entry["correlation_id"]) == (
            error["traceId"],
            error["correlationId"],
        )
    assert "secret internal detail" in unhandled["exception"]


@pytest.mark.unit
def test_a_generated_correlation_id_is_the_one_in_the_logs_and_the_audit_record(
    tmp_path, monkeypatch, log_entries
) -> None:
    body = {
        "metadata": {"name": "llama", "namespace": "default"},
        "spec": {
            "model": {"reference": "models/llama"},
            "runtime": {"provider": "vllm"},
            "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}},
        },
    }
    with TestClient(_dev_app(tmp_path)) as client:
        registry = get_registry()
        put = registry.put

        async def logged_put(*args, **kwargs):
            logging.getLogger("forgemesh.test").warning("creating")
            return await put(*args, **kwargs)

        monkeypatch.setattr(registry, "put", logged_put)
        created = client.post("/api/v1/inference-services", json=body, headers=HEADERS)
        (logged,) = [entry for entry in log_entries() if entry["message"] == "creating"]
        audit = client.get(
            "/api/v1/audit-events",
            params={"correlationId": logged["correlation_id"]},
            headers=HEADERS,
        )

    assert created.status_code == 201
    assert HEX32.fullmatch(logged["correlation_id"])
    assert [(item["action"], item["resourceId"]) for item in audit.json()["items"]] == [
        ("create", "llama")
    ]
