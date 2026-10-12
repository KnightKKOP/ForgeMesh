"""控制平面闭环 E2E 测试：应用 + 一块设备的加速器清单 + 假启动器，不需要 GPU。

* 创建 → ``Ready``：``status.endpoints`` 指向假实例，向这个地址发出的流式对话以
  ``data: [DONE]`` 结束；
* 删除 → 实例停止（端口拒绝连接）、分配释放、资源消失；同名服务重新创建后用同一块设备
  再次 ``Ready``（另一个实例）；
* 重启控制平面：实例随旧进程结束，``Ready`` 服务转为 ``Degraded``（``Ready=False``、
  ``InstanceLost``，端点不再就绪），删除后资源消失、分配释放。

经网关转发（``/api/v1/namespaces/{ns}/inference/{svc}/chat``）的一段由 compose 冒烟
``scripts/compose_smoke.sh`` 覆盖。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from forgemesh.controlplane import app as app_module
from forgemesh.controlplane.api.dependencies import reset_runtime_components
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.auth import AUTH_MODE_DEV, AUTH_MODE_ENV
from forgemesh.controlplane.deployment.config import (
    FAKE_ADVERTISE_HOST_ENV,
    FAKE_BIND_HOST_ENV,
    RUNTIME_LAUNCHER_ENV,
)
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.controlplane.scheduler import INVENTORY_FILE_ENV

TENANT = "acme"
HEADERS = {"X-Tenant-Id": TENANT}
SERVICES = "/api/v1/inference-services"
SERVICE = f"{SERVICES}/llama"
PARAMS = {"namespace": "default"}
BODY = {
    "kind": "InferenceService",
    "metadata": {"name": "llama", "namespace": "default"},
    "spec": {
        "model": {"reference": "models/llama"},
        "runtime": {"provider": "vllm"},
        "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}},
    },
}
CHAT = {"model": "llama", "messages": [{"role": "user", "content": "Hello"}], "stream": True}
DEVICE = {"node_id": "node-a", "device_id": "gpu-0"}
TIMEOUT_SECONDS = 10.0


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """配置一块设备的清单与假启动器（监听回环地址），返回控制平面的数据库路径。"""
    inventory = tmp_path / "inventory.json"
    gpu = {"id": "gpu-0", "type": "nvidia-gpu", "memoryGiB": 80}
    inventory.write_text(json.dumps({"nodes": [{"id": "node-a", "devices": [gpu]}]}))
    monkeypatch.setenv(AUTH_MODE_ENV, AUTH_MODE_DEV)
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(inventory))
    monkeypatch.setenv(RUNTIME_LAUNCHER_ENV, "fake")
    monkeypatch.delenv(FAKE_BIND_HOST_ENV, raising=False)
    monkeypatch.delenv(FAKE_ADVERTISE_HOST_ENV, raising=False)
    monkeypatch.setattr(app_module, "CONTROLLER_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "WORKFLOW_POLL_INTERVAL_SECONDS", 0.05)
    reset_runtime_components()
    yield tmp_path / "cp.db"
    reset_runtime_components()


def _get(client: TestClient) -> dict[str, Any] | None:
    response = client.get(SERVICE, params=PARAMS, headers=HEADERS)
    if response.status_code == 404:
        return None
    assert response.status_code == 200, response.text
    return response.json()


def _wait(
    client: TestClient, done: Callable[[dict[str, Any] | None], bool], what: str
) -> dict[str, Any] | None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        service = _get(client)
        if done(service):
            return service
        assert time.monotonic() < deadline, f"timed out waiting for {what}: {service}"
        time.sleep(0.02)


def _deploy(client: TestClient) -> dict[str, Any]:
    """创建服务并等它离开部署中的 phase；断言它 ``Ready``。"""
    created = client.post(SERVICES, json=BODY, headers=HEADERS)
    assert created.status_code == 201, created.text
    service = _wait(
        client,
        lambda s: s is not None and s["status"].get("phase") in ("Ready", "Failed"),
        "the service to be deployed",
    )
    assert service is not None
    assert service["status"]["phase"] == "Ready", service["status"]
    return service


def _delete(client: TestClient) -> int:
    """删除服务并等它从注册表中消失，返回 DELETE 的状态码。"""
    response = client.delete(SERVICE, params=PARAMS, headers=HEADERS)
    _wait(client, lambda s: s is None, "the service to be deleted")
    return response.status_code


def _ready_condition(service: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in service["status"]["conditions"] if c["type"] == "Ready")


def _chat(url: str) -> tuple[int, str, list[str]]:
    """向实例地址发出流式对话，返回 ``(状态码, Content-Type, 每个事件的 data)``。"""
    parts = urlsplit(url)
    assert parts.hostname is not None
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(CHAT),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
    finally:
        connection.close()
    events = [event.removeprefix("data: ") for event in body.split("\n\n") if event]
    return response.status, response.getheader("Content-Type", ""), events


def _text(events: list[str]) -> str:
    chunks = [json.loads(event) for event in events[:-1]]
    return "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)


def _refused(url: str) -> bool:
    parts = urlsplit(url)
    assert parts.hostname is not None and parts.port is not None
    try:
        socket.create_connection((parts.hostname, parts.port), timeout=1).close()
    except ConnectionRefusedError:
        return True
    return False


async def _allocations(db: Path) -> list[dict[str, Any]]:
    registry = SQLiteRegistry(str(db))
    try:
        return await registry.list_allocations(TENANT)
    finally:
        await registry.close()


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.e2e
def test_a_service_is_deployed_served_deleted_and_deployed_again(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(create_app(db_path=str(db_path))) as client:
        first = _deploy(client)
        [endpoint] = first["status"]["endpoints"]
        status, content_type, events = _chat(endpoint["url"])
        deleted = _delete(client)
        stopped = _refused(endpoint["url"])

        again = _deploy(client)
        [endpoint_again] = again["status"]["endpoints"]
        status_again, _, events_again = _chat(endpoint_again["url"])

    ready = _ready_condition(first)
    assert (ready["status"], ready["reason"]) == ("True", "Reconciled")
    assert endpoint["ready"] is True
    assert endpoint["protocol"] == "openai-chat"
    assert endpoint["instanceId"].startswith("fake-")
    assert urlsplit(endpoint["url"]).hostname == "127.0.0.1"
    assert (status, content_type) == (200, "text/event-stream")
    assert events[-1] == "[DONE]"
    assert _text(events) == f"Hello from the ForgeMesh fake instance {endpoint['instanceId']}."

    assert deleted == 202
    assert stopped

    assert endpoint_again["instanceId"] != endpoint["instanceId"]
    assert (status_again, events_again[-1]) == (200, "[DONE]")
    assert _refused(endpoint_again["url"])
    allocations = asyncio.run(_allocations(db_path))
    assert sorted(a["status"] for a in allocations) == ["Active", "Released"]
    assert [a["devices"] for a in allocations] == [[DEVICE], [DEVICE]]
    assert _errors(caplog) == []


@pytest.mark.e2e
def test_a_ready_service_whose_instance_ended_with_the_control_plane_is_degraded(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(create_app(db_path=str(db_path))) as client:
        [endpoint] = _deploy(client)["status"]["endpoints"]
    stopped_with_the_control_plane = _refused(endpoint["url"])

    with TestClient(create_app(db_path=str(db_path))) as client:
        degraded = _get(client)
        deleted = _delete(client)

    assert stopped_with_the_control_plane
    assert degraded is not None
    assert degraded["status"]["phase"] == "Degraded"
    assert degraded["status"]["endpoints"] == [{**endpoint, "ready": False}]
    ready = _ready_condition(degraded)
    assert (ready["status"], ready["reason"]) == ("False", "InstanceLost")
    assert ready["message"] == (
        f"instance {endpoint['instanceId']} at {endpoint['url']} did not survive the control "
        "plane restart; delete and re-create the service to deploy it again"
    )
    assert deleted == 202
    assert [a["status"] for a in asyncio.run(_allocations(db_path))] == ["Released"]
    assert db_path.with_name("cp.workflows.db").exists()
    assert _errors(caplog) == []
