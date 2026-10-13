"""REST API 单元测试。

覆盖 ``/api/v1`` 资源 CRUD 端点与租户上下文注入：

* 创建 → 查询往返；
* 重复创建返回 409；
* 查询不存在返回 404；
* 删除：无副作用且没有分配的资源 204 直接物理删除；有副作用态（Ready）或持有
  加速器分配的资源 202 Terminating；
* 列表返回多个资源；
* 缺失 ``X-Tenant-Id`` 头返回 401（dev 认证模式）。

使用 FastAPI ``TestClient``（同步驱动），不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from forgemesh.controlplane.api import create_api_router
from forgemesh.controlplane.api.dependencies import (
    ComponentNotReadyError,
    get_registry,
    init_registry,
    reset_registry,
)
from forgemesh.controlplane.api.errors import install_exception_handlers
from forgemesh.core.objects import ObjectMeta
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

TENANT_HEADERS = {"X-Tenant-Id": "tenant-a"}


@pytest.fixture()
def client() -> TestClient:
    """构造带 API 路由的测试客户端（全新内存库）。"""
    init_registry(":memory:")
    app = FastAPI()
    app.include_router(create_api_router())
    return TestClient(app)


SPEC = {
    "model": {"reference": "models/llama"},
    "runtime": {"provider": "vllm", "version": "0.6.3"},
    "resources": {"accelerator": {"type": "nvidia-gpu", "count": 1, "minMemoryGiB": 16}},
}


def _payload(name: str = "svc-1", namespace: str = "default") -> dict:
    """构造推理服务创建请求体（OpenAPI 要求必须携带 spec）。"""
    return {
        "apiVersion": "forgemesh.io/v1alpha1",
        "kind": "InferenceService",
        "metadata": {"name": name, "namespace": namespace},
        "spec": SPEC,
    }


def test_create_and_get_inference_service(client: TestClient) -> None:
    """POST 创建 → GET 查询返回同一资源。"""
    resp = client.post("/api/v1/inference-services", json=_payload(), headers=TENANT_HEADERS)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["kind"] == "InferenceService"
    assert created["metadata"]["name"] == "svc-1"
    assert created["metadata"]["tenantId"] == "tenant-a"

    resp2 = client.get(
        "/api/v1/inference-services/svc-1",
        headers=TENANT_HEADERS,
        params={"namespace": "default"},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["metadata"]["name"] == "svc-1"


def test_create_conflict(client: TestClient) -> None:
    """重复创建同名资源返回 409。"""
    client.post("/api/v1/inference-services", json=_payload(), headers=TENANT_HEADERS)
    resp = client.post("/api/v1/inference-services", json=_payload(), headers=TENANT_HEADERS)
    assert resp.status_code == 409, resp.text


def test_get_not_found(client: TestClient) -> None:
    """查询不存在的资源返回 404。"""
    resp = client.get(
        "/api/v1/inference-services/nope",
        headers=TENANT_HEADERS,
        params={"namespace": "default"},
    )
    assert resp.status_code == 404, resp.text


def test_delete_inference_service(client: TestClient) -> None:
    """无副作用资源 DELETE → 204 直接物理删除；随后 GET 404。"""
    client.post("/api/v1/inference-services", json=_payload(), headers=TENANT_HEADERS)
    resp = client.delete(
        "/api/v1/inference-services/svc-1",
        headers=TENANT_HEADERS,
        params={"namespace": "default"},
    )
    assert resp.status_code == 204, resp.text

    resp2 = client.get(
        "/api/v1/inference-services/svc-1",
        headers=TENANT_HEADERS,
        params={"namespace": "default"},
    )
    assert resp2.status_code == 404, resp2.text


def test_delete_terminating_returns_202(client: TestClient) -> None:
    """有副作用态（Ready）资源 DELETE → 202 + Terminating（受控删除受理）。"""
    # 通过全局 registry 写入带 status.phase=Ready 的强类型资源
    # （API 创建的基类资源无 phase，走 204 路径）。
    registry = get_registry()
    svc = InferenceService(
        metadata=ObjectMeta(
            name="svc-ready", namespace="default", tenant_id="tenant-a", generation=1
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=InferencePhase.READY),
    )
    asyncio.run(registry.put(svc))

    resp = client.delete(
        "/api/v1/inference-services/svc-ready",
        headers=TENANT_HEADERS,
        params={"namespace": "default"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["name"] == "svc-ready"
    assert body["phase"] == "Terminating"
    # 资源仍存在（等待协调器受控释放后物理删除）
    row = registry._conn.execute("SELECT phase FROM resources WHERE name='svc-ready'").fetchone()
    assert row is not None and row["phase"] == "Terminating"


def test_delete_pending_service_holding_an_allocation_returns_202(client: TestClient) -> None:
    """Pending 资源持有加速器分配 → 202 Terminating：分配由协调器释放后才物理删除。"""
    created = client.post("/api/v1/inference-services", json=_payload(), headers=TENANT_HEADERS)
    uid = created.json()["metadata"]["uid"]
    owner = ResourceRef(kind="InferenceService", namespace="default", name="svc-1", uid=uid)
    device = AllocatedDevice(node_id="node-a", device_id="gpu-0")
    asyncio.run(get_registry().create_allocation("tenant-a", owner, f"{uid}:1", [device]))

    resp = client.delete(
        "/api/v1/inference-services/svc-1",
        headers=TENANT_HEADERS,
        params={"namespace": "default"},
    )

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"name": "svc-1", "phase": "Terminating"}


def test_list_inference_services(client: TestClient) -> None:
    """创建多个 → LIST 返回列表。"""
    for i in range(3):
        client.post(
            "/api/v1/inference-services",
            json=_payload(name=f"svc-{i}"),
            headers=TENANT_HEADERS,
        )
    resp = client.get("/api/v1/inference-services", headers=TENANT_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    assert len(body["items"]) == 3


def test_missing_tenant_header(client: TestClient) -> None:
    """dev 认证模式下无 X-Tenant-Id 头返回 401，且不落库。"""
    resp = client.post("/api/v1/inference-services", json=_payload())
    assert resp.status_code == 401, resp.text
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")
    assert asyncio.run(get_registry().list("InferenceService", "tenant-a")) == []


def test_create_preserves_spec_and_assigns_server_fields(client: TestClient) -> None:
    """spec 原样持久化；uid 由服务端分配；status 由控制器维护，请求中的值被忽略。"""
    body = _payload()
    body["metadata"]["uid"] = "client-chosen"
    body["status"] = {"phase": "Ready"}
    resp = client.post("/api/v1/inference-services", json=body, headers=TENANT_HEADERS)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["spec"]["model"] == {"reference": "models/llama", "digest": None}
    assert created["spec"]["runtime"] == {"provider": "vllm", "version": "0.6.3"}
    assert created["metadata"]["uid"] not in (None, "client-chosen")
    assert created["status"]["phase"] == "Pending"

    fetched = client.get(
        "/api/v1/inference-services/svc-1", headers=TENANT_HEADERS, params={"namespace": "default"}
    ).json()
    assert fetched["spec"] == created["spec"]
    assert fetched["metadata"]["uid"] == created["metadata"]["uid"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda b: b.pop("spec"), id="missing-spec"),
        pytest.param(lambda b: b.update(kind="TotallyNotAnInferenceService"), id="wrong-kind"),
        pytest.param(lambda b: b.update(metadata="svc-1"), id="metadata-not-object"),
        pytest.param(lambda b: b["spec"].update(replicas=4), id="unknown-spec-field"),
    ],
)
def test_create_rejects_invalid_body(client: TestClient, mutate) -> None:
    """不合法请求体返回 400 错误信封，且不落库。"""
    body = _payload()
    body["spec"] = dict(SPEC)
    mutate(body)
    resp = client.post("/api/v1/inference-services", json=body, headers=TENANT_HEADERS)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
    listed = client.get("/api/v1/inference-services", headers=TENANT_HEADERS).json()
    assert listed["count"] == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/inference-services"),
        ("POST", "/api/v1/inference-services"),
        ("GET", "/api/v1/inference-services/svc-1"),
        ("DELETE", "/api/v1/inference-services/svc-1"),
        ("GET", "/api/v1/resources?kind=InferenceService"),
    ],
)
def test_routes_report_an_uninitialised_registry(method: str, path: str) -> None:
    """Registry 未初始化：返回 503 错误信封，不以内存库兜底；认证仍先于它检查。"""
    reset_registry()
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_api_router())
    client = TestClient(app)
    body = _payload() if method == "POST" else None

    resp = client.request(
        method, path, json=body, headers={**TENANT_HEADERS, "X-Correlation-Id": "corr-1"}
    )
    unauthenticated = client.request(method, path, json=body)

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert (error["code"], error["correlationId"]) == ("REGISTRY_UNAVAILABLE", "corr-1")
    assert unauthenticated.status_code == 401
    with pytest.raises(ComponentNotReadyError):
        get_registry()


@pytest.mark.parametrize("path", ["/api/v1/inference-services", "/api/v1/resources"])
@pytest.mark.parametrize("limit", [-1, 0, 1001])
def test_list_rejects_out_of_range_limit(client: TestClient, path: str, limit: int) -> None:
    """limit 超出 [1, 1000] 返回 400（SQLite 中 LIMIT -1 表示不限条数）。"""
    resp = client.get(
        path, headers=TENANT_HEADERS, params={"limit": limit, "kind": "InferenceService"}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
