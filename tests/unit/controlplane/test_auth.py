"""控制平面认证测试：凭证校验、租户绑定、scope、配置与启动约束。"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from forgemesh.controlplane import app as app_module
from forgemesh.controlplane.api import create_api_router
from forgemesh.controlplane.api.dependencies import (
    get_registry,
    init_registry,
    reset_authenticator,
)
from forgemesh.controlplane.api.errors import install_exception_handlers
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.auth import (
    API_KEYS_FILE_ENV,
    AUTH_MODE_ENV,
    ApiKeyAuthenticator,
    ApiKeyEntry,
    AuthConfigError,
    AuthenticationError,
    HeaderAuthenticator,
    PermissionDeniedError,
    authenticator_from_env,
    hash_api_key,
    load_api_keys,
)
from forgemesh.controlplane.keygen import main as keygen_main

KEY_A = "fmk_key-for-tenant-a"
KEY_B = "fmk_key-for-tenant-b"
KEY_READ_ONLY = "fmk_read-only-key-for-tenant-a"

ENTRIES = [
    ApiKeyEntry(id="a", sha256=hash_api_key(KEY_A), tenantId="tenant-a", subject="alice"),
    ApiKeyEntry(id="b", sha256=hash_api_key(KEY_B), tenantId="tenant-b", subject="bob"),
    ApiKeyEntry(
        id="ro",
        sha256=hash_api_key(KEY_READ_ONLY),
        tenantId="tenant-a",
        subject="viewer",
        scopes=("read",),
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


def _bearer(key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **extra}


@pytest.fixture()
def api_client(tmp_path):
    app = create_app(db_path=str(tmp_path / "cp.db"), authenticator=ApiKeyAuthenticator(ENTRIES))
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ #
# 认证器
# ------------------------------------------------------------------ #


@pytest.mark.unit
def test_header_authenticator_requires_a_tenant() -> None:
    with pytest.raises(AuthenticationError):
        HeaderAuthenticator().authenticate({})
    principal = HeaderAuthenticator().authenticate({"x-tenant-id": " acme "})
    assert principal.tenant_id == "acme"
    assert principal.has_scope("write")
    assert principal.key_id is None


@pytest.mark.unit
def test_api_key_binds_tenant_subject_and_scopes() -> None:
    authenticator = ApiKeyAuthenticator(ENTRIES)

    principal = authenticator.authenticate(_bearer(KEY_A))
    read_only = authenticator.authenticate(_bearer(KEY_READ_ONLY))

    assert (principal.tenant_id, principal.subject, principal.key_id) == ("tenant-a", "alice", "a")
    assert principal.has_scope("read") and principal.has_scope("write")
    assert read_only.has_scope("read") and not read_only.has_scope("write")


@pytest.mark.unit
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Tenant-Id": "tenant-a"},
        {"Authorization": f"Basic {KEY_A}"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer fmk_unknown"},
    ],
)
def test_api_key_rejects_missing_malformed_and_unknown_credentials(headers) -> None:
    with pytest.raises(AuthenticationError):
        ApiKeyAuthenticator(ENTRIES).authenticate(headers)


@pytest.mark.unit
def test_bearer_scheme_is_case_insensitive() -> None:
    principal = ApiKeyAuthenticator(ENTRIES).authenticate({"authorization": f"bearer {KEY_B}"})
    assert principal.tenant_id == "tenant-b"


@pytest.mark.unit
def test_tenant_header_must_match_the_credentials() -> None:
    authenticator = ApiKeyAuthenticator(ENTRIES)

    assert authenticator.authenticate(_bearer(KEY_A, **{"X-Tenant-Id": " tenant-a "}))
    with pytest.raises(PermissionDeniedError):
        authenticator.authenticate(_bearer(KEY_A, **{"X-Tenant-Id": "tenant-b"}))


@pytest.mark.unit
def test_duplicate_digests_or_ids_are_rejected() -> None:
    duplicate_digest = ENTRIES[0].model_copy(update={"id": "other"})
    duplicate_id = ENTRIES[1].model_copy(update={"id": "a"})
    with pytest.raises(AuthConfigError):
        ApiKeyAuthenticator([ENTRIES[0], duplicate_digest])
    with pytest.raises(AuthConfigError):
        ApiKeyAuthenticator([ENTRIES[0], duplicate_id])


# ------------------------------------------------------------------ #
# key 文件与环境变量
# ------------------------------------------------------------------ #


def _write_keys(path, entries: list[dict]) -> str:
    path.write_text(json.dumps({"keys": entries}), encoding="utf-8")
    return str(path)


@pytest.mark.unit
def test_load_api_keys_normalizes_digests(tmp_path) -> None:
    keys_file = _write_keys(
        tmp_path / "keys.json",
        [{"id": "a", "sha256": hash_api_key(KEY_A).upper(), "tenantId": "t", "subject": "s"}],
    )
    (entry,) = load_api_keys(keys_file)
    assert entry.sha256 == hash_api_key(KEY_A)
    assert entry.scopes == ("read", "write")


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"keys": [{"id": "a", "sha256": "abc", "tenantId": "t", "subject": "s"}]}),
        json.dumps(
            {
                "keys": [
                    {
                        "id": "a",
                        "sha256": "0" * 64,
                        "tenantId": "t",
                        "subject": "s",
                        "scopes": ["admin"],
                    }
                ]
            }
        ),
        json.dumps({"keys": [{"id": "a", "sha256": "0" * 64, "tenantId": "t"}]}),
        json.dumps({"keys": [{"id": "a", "sha256": "0" * 64, "tenant_id": "t", "subject": "s"}]}),
    ],
)
def test_load_api_keys_rejects_invalid_files(tmp_path, content) -> None:
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(content, encoding="utf-8")
    with pytest.raises(AuthConfigError):
        load_api_keys(keys_file)


@pytest.mark.unit
def test_load_api_keys_reports_unreadable_files(tmp_path) -> None:
    with pytest.raises(AuthConfigError):
        load_api_keys(tmp_path / "missing.json")


@pytest.mark.unit
def test_authenticator_from_env(tmp_path) -> None:
    keys_file = _write_keys(
        tmp_path / "keys.json",
        [{"id": "a", "sha256": hash_api_key(KEY_A), "tenantId": "tenant-a", "subject": "s"}],
    )

    with pytest.raises(AuthConfigError):
        authenticator_from_env({})
    with pytest.raises(AuthConfigError):
        authenticator_from_env({AUTH_MODE_ENV: "header"})
    assert isinstance(authenticator_from_env({AUTH_MODE_ENV: "dev"}), HeaderAuthenticator)
    configured = authenticator_from_env({API_KEYS_FILE_ENV: keys_file})
    assert configured.authenticate(_bearer(KEY_A)).tenant_id == "tenant-a"


@pytest.mark.unit
def test_keygen_rejects_invalid_parameters(capsys) -> None:
    assert keygen_main(["--tenant", "", "--subject", "ci"]) == 2
    assert "invalid key parameters" in capsys.readouterr().err


@pytest.mark.unit
def test_keygen_prints_a_key_matching_its_entry(capsys) -> None:
    assert keygen_main(["--tenant", "acme", "--subject", "ci", "--scope", "read"]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    key, entry = lines[1], ApiKeyEntry.model_validate_json(lines[-1])
    assert key.startswith("fmk_")
    assert entry.sha256 == hash_api_key(key)
    assert (entry.id, entry.tenant_id, entry.subject, entry.scopes) == (
        "acme-ci",
        "acme",
        "ci",
        ("read",),
    )


# ------------------------------------------------------------------ #
# API 行为
# ------------------------------------------------------------------ #


@pytest.mark.unit
def test_requests_without_valid_credentials_are_rejected(api_client) -> None:
    for headers in ({}, {"X-Tenant-Id": "tenant-a"}, _bearer("fmk_unknown")):
        response = api_client.get("/api/v1/inference-services", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.unit
def test_tenant_comes_from_the_credentials_not_the_header(api_client) -> None:
    created = api_client.post("/api/v1/inference-services", json=BODY, headers=_bearer(KEY_A))
    spoofed = api_client.post(
        "/api/v1/inference-services",
        json=BODY,
        headers=_bearer(KEY_A, **{"X-Tenant-Id": "tenant-b"}),
    )
    path = "/api/v1/inference-services/llama"
    params = {"namespace": "default"}
    own = api_client.get(path, params=params, headers=_bearer(KEY_A))
    other = api_client.get(path, params=params, headers=_bearer(KEY_B))
    other_list = api_client.get("/api/v1/inference-services", headers=_bearer(KEY_B))

    assert created.status_code == 201
    assert created.json()["metadata"]["tenantId"] == "tenant-a"
    assert spoofed.status_code == 403
    assert spoofed.json()["error"]["code"] == "FORBIDDEN"
    assert own.status_code == 200
    assert other.status_code == 404
    assert other_list.json()["count"] == 0


@pytest.mark.unit
def test_read_only_credentials_cannot_write(api_client) -> None:
    api_client.post("/api/v1/inference-services", json=BODY, headers=_bearer(KEY_A))

    listed = api_client.get("/api/v1/inference-services", headers=_bearer(KEY_READ_ONLY))
    created = api_client.post(
        "/api/v1/inference-services",
        json={**BODY, "metadata": {"name": "other", "namespace": "default"}},
        headers=_bearer(KEY_READ_ONLY),
    )
    deleted = api_client.delete(
        "/api/v1/inference-services/llama",
        params={"namespace": "default"},
        headers=_bearer(KEY_READ_ONLY),
    )
    replaced = api_client.put(
        "/api/v1/inference-services/llama",
        params={"namespace": "default"},
        json=BODY,
        headers=_bearer(KEY_READ_ONLY),
    )
    patched = api_client.patch(
        "/api/v1/inference-services/llama",
        params={"namespace": "default"},
        content=b'{"metadata": {"labels": {"tier": "gold"}}}',
        headers=_bearer(KEY_READ_ONLY, **{"Content-Type": "application/merge-patch+json"}),
    )

    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert created.status_code == 403
    assert deleted.status_code == 403
    assert (replaced.status_code, patched.status_code) == (403, 403)
    assert api_client.get("/api/v1/inference-services", headers=_bearer(KEY_A)).json()["count"] == 1


@pytest.mark.unit
def test_api_writes_are_audited_with_the_principal(api_client) -> None:
    created = api_client.post(
        "/api/v1/inference-services",
        json=BODY,
        headers=_bearer(KEY_A, **{"X-Correlation-Id": "corr-create"}),
    )
    deleted = api_client.delete(
        "/api/v1/inference-services/llama",
        params={"namespace": "default"},
        headers=_bearer(KEY_A, **{"X-Correlation-Id": "corr-delete"}),
    )

    assert (created.status_code, deleted.status_code) == (201, 204)
    registry = get_registry()
    records = sorted(registry.audit_log.query_by_tenant("tenant-a"), key=lambda e: e.timestamp)
    assert [(r.action, r.correlation_id) for r in records] == [
        ("create", "corr-create"),
        ("delete", "corr-delete"),
    ]
    for record in records:
        assert (record.tenant_id, record.actor, record.key_id) == ("tenant-a", "alice", "a")
        assert (record.resource_type, record.namespace, record.resource_id) == (
            "InferenceService",
            "default",
            "llama",
        )
        event = registry.outbox.get(record.details["eventId"])
        assert event is not None
        assert event.payload["correlationId"] == record.correlation_id
    assert registry.audit_log.query_by_tenant("tenant-b") == []


@pytest.mark.unit
def test_updates_are_audited_with_the_principal(api_client) -> None:
    path = "/api/v1/inference-services/llama"
    namespace = {"namespace": "default"}
    document = api_client.post(
        "/api/v1/inference-services", json=BODY, headers=_bearer(KEY_A)
    ).json()
    # 不以创建时的版本为前置条件：控制器可能已写入 status。
    del document["metadata"]["resourceVersion"]
    document["spec"]["model"]["reference"] = "models/mistral"

    replaced = api_client.put(
        path, params=namespace, json=document, headers=_bearer(KEY_A, **{"X-Correlation-Id": "c-1"})
    )
    patched = api_client.patch(
        path,
        params=namespace,
        content=json.dumps({"metadata": {"labels": {"team": "search"}}}),
        headers=_bearer(
            KEY_A, **{"Content-Type": "application/merge-patch+json", "X-Correlation-Id": "c-2"}
        ),
    )

    assert (replaced.status_code, patched.status_code) == (200, 200)
    create, *updates = sorted(
        get_registry().audit_log.query_by_tenant("tenant-a"), key=lambda e: e.timestamp
    )
    assert create.action == "create"
    assert [(r.action, r.correlation_id) for r in updates] == [("update", "c-1"), ("update", "c-2")]
    for record, response in zip(updates, (replaced, patched), strict=True):
        assert (record.actor, record.key_id, record.namespace, record.resource_id) == (
            "alice",
            "a",
            "default",
            "llama",
        )
        version = int(response.json()["metadata"]["resourceVersion"])
        assert record.details["resourceVersion"] == version


@pytest.mark.unit
def test_rejected_api_writes_are_not_audited(api_client) -> None:
    path = "/api/v1/inference-services"
    api_client.post(path, json=BODY, headers=_bearer(KEY_A))

    duplicate = api_client.post(path, json=BODY, headers=_bearer(KEY_A))
    read_only = api_client.delete(
        f"{path}/llama", params={"namespace": "default"}, headers=_bearer(KEY_READ_ONLY)
    )
    missing = api_client.delete(f"{path}/missing", headers=_bearer(KEY_A))

    assert (duplicate.status_code, read_only.status_code, missing.status_code) == (409, 403, 404)
    [record] = get_registry().audit_log.query_by_tenant("tenant-a")
    assert (record.action, record.actor) == ("create", "alice")


@pytest.mark.unit
def test_missing_authenticator_fails_closed() -> None:
    init_registry(":memory:")
    reset_authenticator()
    bare = FastAPI()
    install_exception_handlers(bare)
    bare.include_router(create_api_router())

    response = TestClient(bare).get("/api/v1/inference-services", headers={"X-Tenant-Id": "t"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTHENTICATOR_UNAVAILABLE"


@pytest.mark.unit
def test_app_refuses_to_start_without_an_api_key_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(AUTH_MODE_ENV, raising=False)
    monkeypatch.delenv(API_KEYS_FILE_ENV, raising=False)

    with pytest.raises(AuthConfigError), TestClient(create_app(db_path=str(tmp_path / "cp.db"))):
        pass


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "host"),
    [("dev", "0.0.0.0"), ("dev", "10.0.0.5"), ("header", "127.0.0.1")],
)
def test_main_refuses_unsafe_auth_configuration(monkeypatch, mode, host) -> None:
    def fail_if_started(*args, **kwargs):
        raise AssertionError("server must not start")

    monkeypatch.setenv(AUTH_MODE_ENV, mode)
    monkeypatch.setenv(app_module.HOST_ENV, host)
    monkeypatch.setattr("uvicorn.run", fail_if_started)

    with pytest.raises(SystemExit):
        app_module.main()
