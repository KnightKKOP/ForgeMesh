"""控制平面 ``GET /metrics``：文本格式合法、不要求认证、HTTP 指标按路由模板计数且不含租户数据。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from forgemesh.controlplane.api.dependencies import get_registry, reset_runtime_components
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.auth import (
    ApiKeyAuthenticator,
    ApiKeyEntry,
    HeaderAuthenticator,
    hash_api_key,
)
from forgemesh.controlplane.deployment import RUNTIME_LAUNCHER_ENV
from forgemesh.controlplane.metrics import CONTENT_TYPE
from forgemesh.controlplane.scheduler import INVENTORY_FILE_ENV

from .exposition import parse

TENANT = "tenant-hidden"
NAMESPACE = "ns-hidden"
HEADERS = {"X-Tenant-Id": TENANT}
BODY = {
    "kind": "InferenceService",
    "metadata": {"name": "secret-llama", "namespace": NAMESPACE},
    "spec": {
        "model": {"reference": "models/secret-llama"},
        "runtime": {"provider": "vllm"},
        "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}},
    },
}
ITEM_ROUTE = "/api/v1/inference-services/{name}"


@pytest.fixture(autouse=True)
def _reset_components(monkeypatch):
    monkeypatch.delenv(INVENTORY_FILE_ENV, raising=False)
    monkeypatch.delenv(RUNTIME_LAUNCHER_ENV, raising=False)
    yield
    reset_runtime_components()


def _dev_app(tmp_path):
    return create_app(db_path=str(tmp_path / "cp.db"), authenticator=HeaderAuthenticator())


@pytest.mark.unit
def test_metrics_are_served_without_credentials_in_the_exposition_format(tmp_path) -> None:
    key = "fmk_metrics-test-key"
    entry = ApiKeyEntry(id="a", sha256=hash_api_key(key), tenantId=TENANT, subject="alice")
    app = create_app(db_path=str(tmp_path / "cp.db"), authenticator=ApiKeyAuthenticator([entry]))
    with TestClient(app) as client:
        assert client.get("/api/v1/inference-services").status_code == 401
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/readyz").json()["status"] == "ok"
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    families = parse(response.text)
    requests = families["forgemesh_http_requests_total"]
    assert requests.type == "counter"
    assert requests.value(method="GET", route="/api/v1/inference-services", status="401") == 1
    assert requests.value(method="GET", route="/healthz", status="200") == 1
    assert requests.value(method="GET", route="/readyz", status="200") == 1
    assert families["forgemesh_http_request_duration_seconds"].type == "histogram"


@pytest.mark.unit
def test_http_metrics_use_route_templates_and_bounded_labels(tmp_path) -> None:
    with TestClient(_dev_app(tmp_path)) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        for name, status in (("secret-llama", 200), ("other-secret", 404)):
            fetched = client.get(
                f"/api/v1/inference-services/{name}",
                params={"namespace": NAMESPACE},
                headers=HEADERS,
            )
            assert fetched.status_code == status
        assert client.get("/no/such/hidden-path").status_code == 404
        assert client.request("PROPFIND", "/healthz").status_code == 405
        text = client.get("/metrics").text

    families = parse(text)
    requests = families["forgemesh_http_requests_total"]
    assert requests.value(method="POST", route="/api/v1/inference-services", status="201") == 1
    assert requests.value(method="GET", route=ITEM_ROUTE, status="200") == 1
    assert requests.value(method="GET", route=ITEM_ROUTE, status="404") == 1
    assert requests.value(method="GET", route="unmatched", status="404") == 1
    assert requests.value(method="OTHER", route="/healthz", status="405") == 1
    duration = families["forgemesh_http_request_duration_seconds"]
    count = "forgemesh_http_request_duration_seconds_count"
    assert duration.value(count, method="GET", route=ITEM_ROUTE) == 2
    for tenant_data in (TENANT, NAMESPACE, "secret-llama", "other-secret", "hidden-path"):
        assert tenant_data not in text
    assert "PROPFIND" not in text


@pytest.mark.unit
def test_unhandled_errors_are_counted_as_500(tmp_path, monkeypatch) -> None:
    async def failing_list(*args, **kwargs):
        raise RuntimeError("boom")

    with TestClient(_dev_app(tmp_path), raise_server_exceptions=False) as client:
        monkeypatch.setattr(get_registry(), "list_page", failing_list)
        assert client.get("/api/v1/inference-services", headers=HEADERS).status_code == 500
        text = client.get("/metrics").text

    requests = parse(text)["forgemesh_http_requests_total"]
    assert requests.value(method="GET", route="/api/v1/inference-services", status="500") == 1
