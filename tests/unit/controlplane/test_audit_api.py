"""审计记录查询测试：``AuditLog.list_page`` 分页与 ``GET /api/v1/audit-events``。

* 最新的在前，时间相同时按 ``audit_id``；逐页取完不重不漏，翻页期间写入的新记录不影响
  后面的页；
* 游标与资源列表的游标一样签名，使用同一个数据库密钥：绑定租户与 correlationId，被篡改、
  换了条件、取自资源列表或来自另一个数据库的游标被拒绝（HTTP 400），重启后仍然有效；
* 端点要求 read scope，返回 API 写入产生的审计记录，响应符合 OpenAPI ``AuditEventList``；
* ``limit`` 的校验与列表接口一致。

游标的签发与校验见 ``test_pagetokens.py``，租户隔离见 ``tests/security/test_tenant_isolation.py``。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from forgemesh.controlplane.api.dependencies import get_registry
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.audit import AuditEvent, AuditLog
from forgemesh.controlplane.auth import ApiKeyAuthenticator, ApiKeyEntry, hash_api_key
from forgemesh.controlplane.pagetokens import InvalidPageTokenError
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.objects import ObjectMeta, Resource
from jsonschema import Draft202012Validator

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
URL = "/api/v1/audit-events"
SERVICES_URL = "/api/v1/inference-services"

KEY_A = "fmk_audit-key-for-tenant-a"
KEY_READ_ONLY = "fmk_audit-read-only-key"
KEY_WRITE_ONLY = "fmk_audit-write-only-key"
ENTRIES = [
    ApiKeyEntry(id="a", sha256=hash_api_key(KEY_A), tenantId=TENANT_A, subject="alice"),
    ApiKeyEntry(
        id="ro",
        sha256=hash_api_key(KEY_READ_ONLY),
        tenantId=TENANT_A,
        subject="viewer",
        scopes=("read",),
    ),
    ApiKeyEntry(
        id="wo",
        sha256=hash_api_key(KEY_WRITE_ONLY),
        tenantId=TENANT_A,
        subject="writer",
        scopes=("write",),
    ),
]

BODY = {
    "kind": "InferenceService",
    "metadata": {"name": "llama", "namespace": "default"},
    "spec": {
        "model": {"reference": "models/llama"},
        "runtime": {"provider": "vllm"},
        "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}},
    },
}

_OPENAPI = yaml.safe_load(
    (Path(__file__).resolve().parents[3] / "api" / "openapi" / "openapi.yaml").read_text(
        encoding="utf-8"
    )
)
#: 以 OpenAPI 文档为根校验，``#/components/...`` 引用在文档内解析。
LIST_SCHEMA = Draft202012Validator({**_OPENAPI, "$ref": "#/components/schemas/AuditEventList"})


def _event(
    resource_id: str,
    timestamp: float,
    *,
    tenant_id: str = TENANT_A,
    correlation_id: str = "corr",
    audit_id: str | None = None,
) -> AuditEvent:
    ids = {"audit_id": audit_id} if audit_id is not None else {}
    return AuditEvent(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        actor="alice",
        action="create",
        resource_type="InferenceService",
        resource_id=resource_id,
        outcome="success",
        timestamp=timestamp,
        **ids,
    )


def _all_pages(log: AuditLog, tenant_id: str, limit: int, **filters: Any) -> list[list[str]]:
    pages: list[list[str]] = []
    token = None
    while True:
        page = log.list_page(tenant_id, limit=limit, page_token=token, **filters)
        pages.append([event.resource_id for event in page.items])
        token = page.next_page_token
        if token is None:
            return pages


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _swap_payload(token: str) -> str:
    """把载荷换成指向更晚位置的合法载荷，保留原签名。"""
    payload, _, signature = token.partition(".")
    position = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    position["ts"] += 1.0
    return f"{_b64(json.dumps(position, separators=(',', ':')).encode())}.{signature}"


def _unsigned(token: str) -> str:
    """不带签名的旧式游标：只有位置与查询条件摘要。"""
    return _b64(json.dumps({"ts": 101.0, "id": "id", "q": "0" * 16}).encode())


# ---------------------------------------------------------------------- #
# 分页查询（AuditLog.list_page）
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_pages_list_the_newest_records_first_without_repeats_or_gaps() -> None:
    log = AuditLog()
    for i in range(7):
        log.record(_event(f"svc-{i}", 100.0 + i))
    log.record(_event("other-tenant", 200.0, tenant_id=TENANT_B))

    assert _all_pages(log, TENANT_A, limit=3) == [
        ["svc-6", "svc-5", "svc-4"],
        ["svc-3", "svc-2", "svc-1"],
        ["svc-0"],
    ]
    assert _all_pages(log, TENANT_A, limit=7) == [[f"svc-{i}" for i in range(6, -1, -1)]]


@pytest.mark.unit
def test_records_with_the_same_timestamp_are_ordered_by_audit_id() -> None:
    log = AuditLog()
    for audit_id in ("b", "e", "a", "d", "c"):
        log.record(_event(f"svc-{audit_id}", 100.0, audit_id=audit_id))
    log.record(_event("older", 99.0))

    assert _all_pages(log, TENANT_A, limit=2) == [
        ["svc-e", "svc-d"],
        ["svc-c", "svc-b"],
        ["svc-a", "older"],
    ]


@pytest.mark.unit
def test_records_written_while_paging_do_not_shift_later_pages() -> None:
    log = AuditLog()
    for i in range(4):
        log.record(_event(f"svc-{i}", 100.0 + i))

    first = log.list_page(TENANT_A, limit=2)
    log.record(_event("newer", 200.0))
    second = log.list_page(TENANT_A, limit=2, page_token=first.next_page_token)

    assert [event.resource_id for event in first.items] == ["svc-3", "svc-2"]
    assert [event.resource_id for event in second.items] == ["svc-1", "svc-0"]
    assert second.next_page_token is None


@pytest.mark.unit
def test_correlation_filter_pages_within_the_tenant() -> None:
    log = AuditLog()
    for i in range(3):
        log.record(_event(f"svc-{i}", 100.0 + i, correlation_id="corr-1"))
        log.record(_event(f"other-{i}", 100.5 + i, correlation_id="corr-2"))
    log.record(_event("other-tenant", 300.0, tenant_id=TENANT_B, correlation_id="corr-1"))

    assert _all_pages(log, TENANT_A, limit=2, correlation_id="corr-1") == [
        ["svc-2", "svc-1"],
        ["svc-0"],
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("issued_for", "used_for"),
    [
        ((TENANT_A, None), (TENANT_B, None)),
        ((TENANT_A, None), (TENANT_A, "corr-1")),
        ((TENANT_A, "corr-1"), (TENANT_A, None)),
        ((TENANT_A, "corr-1"), (TENANT_A, "corr-2")),
        ((TENANT_A, "corr-1"), (TENANT_B, "corr-1")),
    ],
)
def test_page_tokens_are_bound_to_the_tenant_and_the_filter(
    issued_for: tuple[str, str | None], used_for: tuple[str, str | None]
) -> None:
    log = AuditLog()
    for i in range(2):
        log.record(_event(f"svc-{i}", 100.0 + i, correlation_id="corr-1"))
    tenant_id, correlation_id = issued_for
    token = log.list_page(tenant_id, limit=1, correlation_id=correlation_id).next_page_token
    assert token is not None

    tenant_id, correlation_id = used_for
    with pytest.raises(InvalidPageTokenError):
        log.list_page(tenant_id, limit=1, page_token=token, correlation_id=correlation_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_swap_payload, id="swapped-payload"),
        pytest.param(
            lambda token: token[:-1] + ("B" if token.endswith("A") else "A"),
            id="altered-signature",
        ),
        pytest.param(lambda token: token.partition(".")[0], id="missing-signature"),
        pytest.param(lambda token: f"{token}.extra", id="trailing-data"),
        pytest.param(lambda token: f"{token}令", id="non-ascii"),
        pytest.param(lambda token: "", id="empty"),
        pytest.param(lambda token: "not a token", id="garbage"),
        pytest.param(_unsigned, id="unsigned"),
    ],
)
def test_tampered_page_tokens_are_rejected(tamper: Callable[[str], str]) -> None:
    log = AuditLog()
    for i in range(3):
        log.record(_event(f"svc-{i}", 100.0 + i))
    token = log.list_page(TENANT_A, limit=1).next_page_token
    assert token is not None

    with pytest.raises(InvalidPageTokenError):
        log.list_page(TENANT_A, limit=1, page_token=tamper(token))


def _widget(name: str) -> Resource:
    return Resource(kind="Widget", metadata=ObjectMeta(name=name, tenant_id=TENANT_A))


@pytest.mark.unit
def test_resource_list_and_audit_list_tokens_are_not_interchangeable() -> None:
    registry = SQLiteRegistry()
    try:
        for i in range(2):
            asyncio.run(registry.put(_widget(f"w-{i}")))
            registry.audit_log.record(_event(f"svc-{i}", 100.0 + i))
        resource_page = asyncio.run(registry.list_page("Widget", TENANT_A, limit=1))
        audit_page = registry.audit_log.list_page(TENANT_A, limit=1)
        assert resource_page.next_page_token is not None
        assert audit_page.next_page_token is not None

        with pytest.raises(InvalidPageTokenError):
            registry.audit_log.list_page(TENANT_A, page_token=resource_page.next_page_token)
        with pytest.raises(InvalidPageTokenError):
            asyncio.run(
                registry.list_page("Widget", TENANT_A, page_token=audit_page.next_page_token)
            )
    finally:
        asyncio.run(registry.close())


@pytest.mark.unit
def test_the_audit_log_signs_with_the_key_of_the_registry_database(tmp_path) -> None:
    path = str(tmp_path / "cp.db")
    registry = SQLiteRegistry(path)
    try:
        for i in range(3):
            registry.audit_log.record(_event(f"svc-{i}", 100.0 + i))
        token = registry.audit_log.list_page(TENANT_A, limit=1).next_page_token
    finally:
        asyncio.run(registry.close())

    reopened = AuditLog(path)
    try:
        page = reopened.list_page(TENANT_A, limit=5, page_token=token)
        keys = reopened.database.conn.execute("SELECT name FROM registry_meta").fetchall()
    finally:
        reopened.close()

    assert [event.resource_id for event in page.items] == ["svc-1", "svc-0"]
    assert [row["name"] for row in keys] == ["page_token_key"]


@pytest.mark.unit
def test_page_tokens_from_another_database_are_rejected() -> None:
    issuer, other = AuditLog(), AuditLog()
    for log in (issuer, other):
        for i in range(2):
            log.record(_event(f"svc-{i}", 100.0 + i))
    token = issuer.list_page(TENANT_A, limit=1).next_page_token

    with pytest.raises(InvalidPageTokenError):
        other.list_page(TENANT_A, limit=1, page_token=token)


@pytest.mark.unit
def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="limit"):
        AuditLog().list_page(TENANT_A, limit=0)


# ---------------------------------------------------------------------- #
# GET /api/v1/audit-events
# ---------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "cp.db"), authenticator=ApiKeyAuthenticator(ENTRIES))
    with TestClient(app) as test_client:
        yield test_client


def _bearer(key: str = KEY_A, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **extra}


def _get(client: TestClient, key: str = KEY_A, **params: Any) -> Any:
    return client.get(URL, params=params, headers=_bearer(key))


def _ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["resourceId"] for item in response.json()["items"]]


@pytest.mark.unit
def test_lists_the_audit_records_of_api_writes(client: TestClient) -> None:
    created = client.post(
        SERVICES_URL, json=BODY, headers=_bearer(**{"X-Correlation-Id": "corr-create"})
    )
    deleted = client.delete(
        f"{SERVICES_URL}/llama",
        params={"namespace": "default"},
        headers=_bearer(**{"X-Correlation-Id": "corr-delete"}),
    )
    assert (created.status_code, deleted.status_code) == (201, 204)

    response = _get(client)

    assert response.status_code == 200
    body = response.json()
    LIST_SCHEMA.validate(body)
    assert body["count"] == 2
    assert "nextPageToken" not in body
    stored = {e.audit_id: e for e in get_registry().audit_log.query_by_tenant(TENANT_A)}
    newest, oldest = body["items"]
    assert (newest["action"], newest["correlationId"]) == ("delete", "corr-delete")
    assert (oldest["action"], oldest["correlationId"]) == ("create", "corr-create")
    assert (newest["details"]["change"], oldest["details"]["change"]) == ("deleted", "created")
    for item in body["items"]:
        record = stored[item["auditId"]]
        assert (item["tenantId"], item["actor"], item["keyId"]) == (TENANT_A, "alice", "a")
        assert (item["resourceType"], item["namespace"], item["resourceId"]) == (
            "InferenceService",
            "default",
            "llama",
        )
        assert item["outcome"] == "success"
        assert item["details"] == record.details
        assert item["occurredAt"].endswith("Z")
        assert datetime.fromisoformat(item["occurredAt"]) == datetime.fromtimestamp(
            record.timestamp, UTC
        )


@pytest.mark.unit
def test_pages_through_audit_records(client: TestClient) -> None:
    log = get_registry().audit_log
    for i in range(5):
        log.record(_event(f"svc-{i}", 100.0 + i))

    first = _get(client, limit=2).json()
    second = _get(client, limit=2, pageToken=first["nextPageToken"]).json()
    third = _get(client, limit=2, pageToken=second["nextPageToken"]).json()
    resized = _get(client, limit=3, pageToken=first["nextPageToken"])

    for page in (first, second, third):
        LIST_SCHEMA.validate(page)
    assert [[item["resourceId"] for item in page["items"]] for page in (first, second, third)] == [
        ["svc-4", "svc-3"],
        ["svc-2", "svc-1"],
        ["svc-0"],
    ]
    assert [page["count"] for page in (first, second, third)] == [2, 2, 1]
    assert "nextPageToken" not in third
    assert _ids(resized) == ["svc-2", "svc-1", "svc-0"]


@pytest.mark.unit
def test_filters_by_correlation_id(client: TestClient) -> None:
    log = get_registry().audit_log
    log.record(_event("svc-1", 100.0, correlation_id="corr-1"))
    log.record(_event("svc-2", 101.0, correlation_id="corr-2"))

    assert _ids(_get(client, correlationId="corr-1")) == ["svc-1"]
    assert _ids(_get(client, correlationId="unknown")) == []
    assert _ids(_get(client, correlationId="", pageToken="")) == ["svc-2", "svc-1"]


@pytest.mark.unit
def test_rejects_page_tokens_it_cannot_use(client: TestClient) -> None:
    log = get_registry().audit_log
    for i in range(2):
        log.record(_event(f"svc-{i}", 100.0 + i, correlation_id="corr-1"))
    token = _get(client, limit=1).json()["nextPageToken"]
    for name in ("svc-a", "svc-b"):
        body = {**BODY, "metadata": {"name": name, "namespace": "default"}}
        assert client.post(SERVICES_URL, json=body, headers=_bearer()).status_code == 201
    listed = client.get(SERVICES_URL, params={"limit": 1}, headers=_bearer())

    malformed = _get(client, pageToken="garbage")
    other_filter = _get(client, pageToken=token, correlationId="corr-1")
    resource_token = _get(client, pageToken=listed.json()["nextPageToken"])

    for response in (malformed, other_filter, resource_token):
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "BAD_REQUEST"
        assert error["message"].startswith("invalid pageToken")


@pytest.mark.unit
def test_requires_the_read_scope(client: TestClient) -> None:
    anonymous = client.get(URL)
    read_only = _get(client, key=KEY_READ_ONLY)
    write_only = _get(client, key=KEY_WRITE_ONLY)

    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "UNAUTHENTICATED"
    assert read_only.status_code == 200
    assert write_only.status_code == 403
    assert write_only.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.unit
@pytest.mark.parametrize("limit", ["0", "1001", "many"])
def test_rejects_an_invalid_limit(client: TestClient, limit: str) -> None:
    response = _get(client, limit=limit)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
