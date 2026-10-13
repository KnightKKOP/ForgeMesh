"""控制平面 ``GET /metrics``：文本格式合法、不要求认证、HTTP 指标按路由模板计数且不含租户数据；
应用把核对实例、重新部署与回收计入指标。"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from forgemesh.controlplane import app as app_module
from forgemesh.controlplane.api.dependencies import get_registry, reset_runtime_components
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.auth import (
    ApiKeyAuthenticator,
    ApiKeyEntry,
    HeaderAuthenticator,
    hash_api_key,
)
from forgemesh.controlplane.deployment import INSTANCE_CHECK_INTERVAL_ENV, RUNTIME_LAUNCHER_ENV
from forgemesh.controlplane.deployment.config import FAKE_ADVERTISE_HOST_ENV, FAKE_BIND_HOST_ENV
from forgemesh.controlplane.metrics import CONTENT_TYPE
from forgemesh.controlplane.outbox import OutboxEvent
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
    monkeypatch.delenv(INSTANCE_CHECK_INTERVAL_ENV, raising=False)
    yield
    reset_runtime_components()


def _dev_app(tmp_path):
    return create_app(db_path=str(tmp_path / "cp.db"), authenticator=HeaderAuthenticator())


def _enable_fake_deployments(tmp_path, monkeypatch) -> None:
    """两张卡的清单与假启动器（实例监听回环地址）；控制器与部署运行的轮询间隔为 0.05 秒。"""
    inventory = tmp_path / "inventory.json"
    gpus = [{"id": f"gpu-{index}", "type": "nvidia-gpu", "memoryGiB": 80} for index in range(2)]
    inventory.write_text(json.dumps({"nodes": [{"id": "node-hidden", "devices": gpus}]}))
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(inventory))
    monkeypatch.setenv(RUNTIME_LAUNCHER_ENV, "fake")
    monkeypatch.delenv(FAKE_BIND_HOST_ENV, raising=False)
    monkeypatch.delenv(FAKE_ADVERTISE_HOST_ENV, raising=False)
    monkeypatch.setattr(app_module, "CONTROLLER_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "WORKFLOW_POLL_INTERVAL_SECONDS", 0.05)


def _serving(
    client: TestClient, name: str = "secret-llama", other_than: str | None = None
) -> dict[str, Any]:
    """轮询直到 ``name`` 为 Ready 且实例不是 ``other_than``，返回它的 endpoint。"""
    deadline = time.monotonic() + 10
    while True:
        response = client.get(
            f"/api/v1/inference-services/{name}", params={"namespace": NAMESPACE}, headers=HEADERS
        )
        status = response.json()["status"]
        if status["phase"] == "Ready" and status["endpoints"][0]["instanceId"] != other_than:
            return status["endpoints"][0]
        assert time.monotonic() < deadline, f"{name} did not serve a new instance: {status}"
        time.sleep(0.02)


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


@pytest.mark.unit
def test_outbox_metrics_follow_the_event_dispatcher(tmp_path, monkeypatch) -> None:
    """创建事件发布成功、status 写入的事件发布失败：发布尝试按结果计数，失败的事件计为 retrying。"""
    monkeypatch.setattr(app_module, "CONTROLLER_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "EVENT_POLL_INTERVAL_SECONDS", 0.05)

    def publisher(event: OutboxEvent) -> None:
        if event.event_type.endswith(".status_updated"):
            raise ConnectionError("webhook unreachable")

    app = create_app(
        db_path=str(tmp_path / "cp.db"),
        authenticator=HeaderAuthenticator(),
        event_publisher=publisher,
    )
    with TestClient(app) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        deadline = time.monotonic() + 10
        while True:
            families = parse(client.get("/metrics").text)
            attempts = families["forgemesh_outbox_publish_attempts_total"]
            events = families["forgemesh_outbox_events"]
            if (
                attempts.value(outcome="published") >= 1
                and attempts.value(outcome="failed") >= 1
                and events.value(state="retrying") >= 1
            ):
                break
            assert time.monotonic() < deadline, "no event was both published and retried"
            time.sleep(0.02)

    assert attempts.value(outcome="dead") == 0
    assert {s.labels["state"] for s in events.samples} == {"pending", "retrying", "dead"}
    assert events.value(state="dead") == 0


@pytest.mark.unit
def test_a_deployment_shows_up_in_the_controller_and_state_metrics(tmp_path, monkeypatch) -> None:
    _enable_fake_deployments(tmp_path, monkeypatch)

    done = 'forgemesh_reconcile_total{operation="reconcile",outcome="done"}'
    with TestClient(_dev_app(tmp_path)) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        deadline = time.monotonic() + 10
        while done not in (text := client.get("/metrics").text):
            assert time.monotonic() < deadline, "the deployment did not finish"
            time.sleep(0.02)
        service = client.get(
            "/api/v1/inference-services/secret-llama",
            params={"namespace": NAMESPACE},
            headers=HEADERS,
        ).json()

    assert service["status"]["phase"] == "Ready"
    families = parse(text)
    assert families["forgemesh_inference_services"].value(phase="Ready") == 1
    assert families["forgemesh_allocations_active"].value(accelerator_type="nvidia-gpu") == 1
    devices = families["forgemesh_accelerator_devices"]
    assert devices.value(accelerator_type="nvidia-gpu", state="allocated") == 1
    assert devices.value(accelerator_type="nvidia-gpu", state="free") == 1
    assert families["forgemesh_workflow_runs"].value(status="Succeeded") == 1
    deployments = families["forgemesh_deployments_total"]
    assert deployments.value(outcome="ready", reason="Reconciled") == 1
    lock_wait = families["forgemesh_database_lock_wait_seconds"]
    assert lock_wait.value("forgemesh_database_lock_wait_seconds_count") > 0
    assert {s.labels["state"] for s in families["forgemesh_controller_queue_depth"].samples} == {
        "ready",
        "delayed",
        "processing",
    }
    endpoint = service["status"]["endpoints"][0]["url"]
    for tenant_data in (TENANT, NAMESPACE, "secret-llama", "node-hidden", "gpu-", endpoint):
        assert tenant_data not in text


@pytest.mark.unit
def test_an_instance_that_exits_is_counted_by_the_periodic_check_and_redeployed(
    tmp_path, monkeypatch
) -> None:
    """实例在运行中退出：定期核对得出 ``lost``，控制器重新部署一次；启动时的核对没有结论。"""
    _enable_fake_deployments(tmp_path, monkeypatch)
    monkeypatch.setenv(INSTANCE_CHECK_INTERVAL_ENV, "0.05")
    with TestClient(_dev_app(tmp_path)) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        first = _serving(client)
        assert httpx.post(f"{first['url']}/fake/exit", timeout=5).status_code == 202
        _serving(client, other_than=first["instanceId"])
        families = parse(client.get("/metrics").text)

    results = families["forgemesh_instance_check_results_total"]
    assert results.value(check="periodic", result="lost") == 1
    assert results.value(check="startup", result="lost") == 0
    assert families["forgemesh_lost_instance_redeploys_total"].value() == 1
    checks = families["forgemesh_instance_checks_total"]
    assert checks.value(outcome="completed") >= 1
    assert checks.value(outcome="failed") == 0


@pytest.mark.unit
def test_the_startup_check_and_the_reclaim_are_counted_after_a_restart(
    tmp_path, monkeypatch
) -> None:
    """重启之后：实例没能活过重启的服务计入启动时核对的 ``lost`` 并重新部署一次；绕过受控
    删除的服务留下的分配被回收。"""
    _enable_fake_deployments(tmp_path, monkeypatch)
    orphan = {**BODY, "metadata": {**BODY["metadata"], "name": "orphan"}}
    with TestClient(_dev_app(tmp_path)) as client:
        for body in (BODY, orphan):
            created = client.post("/api/v1/inference-services", json=body, headers=HEADERS)
            assert created.status_code == 201
        first = _serving(client)
        _serving(client, "orphan")
        registry = get_registry()
        client.portal.call(registry.delete, "InferenceService", NAMESPACE, "orphan", TENANT)

    with TestClient(_dev_app(tmp_path)) as client:
        _serving(client, other_than=first["instanceId"])
        families = parse(client.get("/metrics").text)

    results = families["forgemesh_instance_check_results_total"]
    assert results.value(check="startup", result="lost") == 1
    assert results.value(check="periodic", result="lost") == 0
    assert families["forgemesh_lost_instance_redeploys_total"].value() == 1
    reclaims = families["forgemesh_orphaned_allocation_reclaims_total"]
    assert (reclaims.value(outcome="reclaimed"), reclaims.value(outcome="failed")) == (1, 0)
