"""控制平面应用入口测试：真实路由已挂载、存储可配置、探针反映存储状态。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from forgemesh.controlplane.api.dependencies import get_registry, reset_runtime_components
from forgemesh.controlplane.app import DB_PATH_ENV, create_app

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


@pytest.fixture(autouse=True)
def _reset_components():
    reset_runtime_components()
    yield
    reset_runtime_components()


@pytest.mark.unit
def test_api_routes_are_served_and_persisted_across_restarts(tmp_path) -> None:
    db_file = tmp_path / "cp.db"

    with TestClient(create_app(db_path=str(db_file))) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201

    with TestClient(create_app(db_path=str(db_file))) as client:
        fetched = client.get(
            "/api/v1/inference-services/llama", params={"namespace": "default"}, headers=HEADERS
        )
        assert fetched.status_code == 200
        assert fetched.json()["metadata"]["name"] == "llama"


@pytest.mark.unit
def test_db_path_comes_from_environment(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "from-env.db"
    monkeypatch.setenv(DB_PATH_ENV, str(db_file))

    with TestClient(create_app()) as client:
        assert client.get("/readyz").status_code == 200

    assert db_file.exists()


@pytest.mark.unit
def test_creating_app_has_no_filesystem_side_effects(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(DB_PATH_ENV, raising=False)

    create_app()

    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_readyz_reports_unavailable_registry(tmp_path) -> None:
    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        assert client.get("/readyz").status_code == 200
        client.portal.call(get_registry().close)
        not_ready = client.get("/readyz")
        assert not_ready.status_code == 503
        assert not_ready.json()["error"]["code"] == "NOT_READY"


@pytest.mark.unit
def test_runtime_endpoints_report_missing_components_instead_of_501(tmp_path) -> None:
    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        response = client.post("/api/v1/decisions", json={"question": "q"}, headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DECISION_ROUTER_UNAVAILABLE"
