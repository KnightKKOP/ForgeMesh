"""控制平面应用入口测试：真实路由已挂载、存储可配置、探针反映存储状态、控制器随应用运行、
配置加速器清单时启用调度器、配置启动器时启用部署、启动时结束上次运行留下的实例进程、定期
核对实例并回收孤立的分配（完整闭环见 ``tests/e2e/test_closed_loop.py``）、Outbox 事件随应用
发布（webhook 见 ``test_publishers.py``）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from forgemesh.controlplane import app as app_module
from forgemesh.controlplane import publishers
from forgemesh.controlplane.api.dependencies import (
    ComponentNotReadyError,
    get_authenticator,
    get_registry,
    reset_registry,
    reset_runtime_components,
)
from forgemesh.controlplane.app import DB_PATH_ENV, create_app
from forgemesh.controlplane.auth import AUTH_MODE_DEV, AUTH_MODE_ENV
from forgemesh.controlplane.controller import ControllerManager
from forgemesh.controlplane.deployment import FakeLauncher, LauncherConfigError
from forgemesh.controlplane.deployment import config as launcher_config
from forgemesh.controlplane.outbox import Dispatcher, Outbox, OutboxEvent, OutboxStatus
from forgemesh.controlplane.publishers import EVENT_LOGGER_NAME, EventConfigError
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.controlplane.scheduler import INVENTORY_FILE_ENV, InventoryConfigError
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import InferenceService
from forgemesh.workflowruntime.runtime import LocalWorkflowRuntime
from forgemesh.workflowruntime.store import WorkflowStore

LAUNCHER_ENVS = [
    getattr(launcher_config, name) for name in launcher_config.__all__ if name.endswith("_ENV")
]
EVENT_ENVS = [getattr(publishers, name) for name in publishers.__all__ if name.endswith("_ENV")]

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
def _reset_components(monkeypatch):
    monkeypatch.setenv(AUTH_MODE_ENV, AUTH_MODE_DEV)
    monkeypatch.delenv(INVENTORY_FILE_ENV, raising=False)
    for name in [*LAUNCHER_ENVS, *EVENT_ENVS]:
        monkeypatch.delenv(name, raising=False)
    reset_runtime_components()
    yield
    reset_runtime_components()


def _write_inventory(tmp_path: Path) -> Path:
    inventory = tmp_path / "inventory.json"
    gpu = {"id": "gpu-0", "type": "nvidia-gpu", "memoryGiB": 80}
    inventory.write_text(json.dumps({"nodes": [{"id": "node-a", "devices": [gpu]}]}))
    return inventory


def _wait_for_ready_condition(client: TestClient, timeout: float = 5.0) -> dict[str, Any]:
    """轮询直到控制器为 ``llama`` 写入 ``Ready`` 条件。"""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(
            "/api/v1/inference-services/llama", params={"namespace": "default"}, headers=HEADERS
        )
        assert response.status_code == 200
        for condition in response.json()["status"]["conditions"]:
            if condition["type"] == "Ready":
                return condition
        assert time.monotonic() < deadline, "the controller did not report a Ready condition"
        time.sleep(0.02)


def _wait_for_instance(
    client: TestClient, other_than: str | None = None, timeout: float = 5.0
) -> dict[str, Any]:
    """轮询直到 ``llama`` 为 Ready，且实例不是 ``other_than``，返回它的 endpoint。"""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(
            "/api/v1/inference-services/llama", params={"namespace": "default"}, headers=HEADERS
        )
        assert response.status_code == 200
        status = response.json()["status"]
        if status["phase"] == "Ready":
            [endpoint] = status["endpoints"]
            if endpoint["instanceId"] != other_than:
                return endpoint
        assert time.monotonic() < deadline, f"llama did not serve a new instance: {status}"
        time.sleep(0.02)


async def _store_service(db_file: Path) -> None:
    registry = SQLiteRegistry(str(db_file))
    try:
        await registry.put(
            InferenceService.model_validate(
                {**BODY, "metadata": {**BODY["metadata"], "tenantId": "acme", "uid": "uid-llama"}}
            )
        )
    finally:
        await registry.close()


async def _allocations(db_file: Path) -> list[dict[str, Any]]:
    registry = SQLiteRegistry(str(db_file))
    try:
        return await registry.list_allocations("acme")
    finally:
        await registry.close()


async def _store_orphaned_allocation(db_file: Path) -> str:
    """存入持有 ``node-a/gpu-0`` 的 ``llama``，再绕过受控删除删掉它，返回留下的分配 id。"""
    await _store_service(db_file)
    registry = SQLiteRegistry(str(db_file))
    try:
        owner = ResourceRef(
            kind="InferenceService", namespace="default", name="llama", uid="uid-llama"
        )
        gpu = AllocatedDevice(node_id="node-a", device_id="gpu-0")
        record = await registry.create_allocation("acme", owner, "uid-llama:1", [gpu])
        await registry.delete("InferenceService", "default", "llama", "acme")
    finally:
        await registry.close()
    return record["id"]


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
def test_readyz_reports_an_uninitialised_registry(tmp_path) -> None:
    """lifespan 没有运行时 Registry 未初始化：就绪探针报告 503，而不是悄悄打开一个内存库。"""
    reset_registry()
    client = TestClient(create_app(db_path=str(tmp_path / "cp.db")))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NOT_READY"
    assert not (tmp_path / "cp.db").exists()


@pytest.mark.unit
def test_stopping_the_app_releases_the_registry(tmp_path) -> None:
    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        assert client.get("/readyz").status_code == 200

    with pytest.raises(ComponentNotReadyError):
        get_registry()


@pytest.mark.unit
def test_framework_errors_use_the_error_envelope(tmp_path) -> None:
    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        missing = client.get(
            "/api/v1/inference-services/nope",
            headers={**HEADERS, "X-Correlation-Id": "corr-1"},
        )
        not_object = client.post("/api/v1/inference-services", json=[1, 2], headers=HEADERS)
        bad_limit = client.get(
            "/api/v1/inference-services", params={"limit": "many"}, headers=HEADERS
        )
        wrong_method = client.put("/healthz")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert missing.json()["error"]["correlationId"] == "corr-1"
    assert not_object.status_code == 400
    assert not_object.json()["error"]["code"] == "BAD_REQUEST"
    assert not_object.json()["error"]["details"]["errors"]
    assert bad_limit.status_code == 400
    assert bad_limit.json()["error"]["code"] == "BAD_REQUEST"
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


@pytest.mark.unit
def test_unhandled_errors_return_an_opaque_500_envelope(tmp_path, monkeypatch) -> None:
    async def failing_list(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    app = create_app(db_path=str(tmp_path / "cp.db"))
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(get_registry(), "list_page", failing_list)
        response = client.get("/api/v1/inference-services", headers=HEADERS)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert "secret" not in error["message"]
    assert error["correlationId"]


@pytest.mark.unit
def test_runtime_endpoints_report_missing_components_instead_of_501(tmp_path) -> None:
    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        response = client.post("/api/v1/decisions", json={"question": "q"}, headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DECISION_ROUTER_UNAVAILABLE"


@pytest.mark.unit
def test_controller_reports_why_a_new_service_is_not_ready(tmp_path) -> None:
    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201

        ready = _wait_for_ready_condition(client)

    assert ready["status"] == "False"
    assert ready["reason"] == "SchedulerUnavailable"


@pytest.mark.unit
def test_controller_picks_up_services_stored_while_it_was_not_running(tmp_path) -> None:
    db_file = tmp_path / "cp.db"
    asyncio.run(_store_service(db_file))

    with TestClient(create_app(db_path=str(db_file))) as client:
        ready = _wait_for_ready_condition(client)

    assert ready["reason"] == "SchedulerUnavailable"


@pytest.mark.unit
def test_controller_schedules_when_an_inventory_is_configured(tmp_path, monkeypatch) -> None:
    """配置清单后不再报告 SchedulerUnavailable；没有设置 FORGEMESH_RUNTIME_LAUNCHER 时不部署，
    协调器在分配之前停在 WorkflowRuntimeUnavailable，不留下分配，也不创建工作流数据库。"""
    db_file = tmp_path / "cp.db"
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))

    with TestClient(create_app(db_path=str(db_file))) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        ready = _wait_for_ready_condition(client)

    assert (ready["status"], ready["reason"]) == ("False", "WorkflowRuntimeUnavailable")
    assert asyncio.run(_allocations(db_file)) == []
    assert not (tmp_path / "cp.workflows.db").exists()


@pytest.mark.unit
def test_a_launcher_enables_deployments_with_their_own_workflow_database(
    tmp_path, monkeypatch
) -> None:
    """部署工作流存放在注册表旁的 ``cp.workflows.db``；``POST /workflows`` 不使用部署的运行时。"""
    db_file = tmp_path / "cp.db"
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
    monkeypatch.setenv(launcher_config.RUNTIME_LAUNCHER_ENV, "fake")

    with TestClient(create_app(db_path=str(db_file))) as client:
        workflows = client.post(
            "/api/v1/workflows",
            json={"definition": "inference-service-deployment", "version": 1},
            headers=HEADERS,
        )

    assert workflows.status_code == 503
    assert workflows.json()["error"]["code"] == "WORKFLOW_RUNTIME_UNAVAILABLE"
    assert (tmp_path / "cp.workflows.db").exists()


@pytest.mark.unit
def test_deployments_recover_before_the_controller_starts_and_stop_after_it(
    tmp_path, monkeypatch
) -> None:
    """启动：结束上次运行留下的实例 → 恢复未完成的部署 → 标记实例已丢失的服务 → 控制器；
    关闭：控制器 → 工作流运行时（其补偿还会调用启动器）→ 启动器 → 工作流存储。"""
    calls: list[str] = []

    def record(owner: Any, name: str, label: str) -> None:
        original = getattr(owner, name)

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls.append(label)
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    record(FakeLauncher, "start", "launcher.start")
    record(LocalWorkflowRuntime, "recover", "runtime.recover")
    record(app_module, "check_instances", "check_instances")
    record(ControllerManager, "start", "controller.start")
    record(ControllerManager, "stop", "controller.stop")
    record(LocalWorkflowRuntime, "aclose", "runtime.aclose")
    record(FakeLauncher, "aclose", "launcher.aclose")
    record(WorkflowStore, "close", "workflow_store.close")
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
    monkeypatch.setenv(launcher_config.RUNTIME_LAUNCHER_ENV, "fake")

    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))):
        started = list(calls)

    assert started == ["launcher.start", "runtime.recover", "check_instances", "controller.start"]
    assert calls[len(started) :] == [
        "controller.stop",
        "runtime.aclose",
        "launcher.aclose",
        "workflow_store.close",
    ]


@pytest.mark.unit
def test_the_app_stops_instance_processes_a_killed_control_plane_left_running(
    tmp_path, monkeypatch
) -> None:
    """进程启动器的实例记录在数据库旁的 ``cp.instances.json`` 中：应用开始服务时，被杀死的
    控制平面留下、仍在运行的实例进程已经结束，记录已删去。"""
    record_file = tmp_path / "cp.instances.json"
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
    monkeypatch.setenv(launcher_config.RUNTIME_LAUNCHER_ENV, "vllm-process")
    monkeypatch.setenv(launcher_config.VLLM_COMMAND_ENV, sys.executable)
    leftover = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True
    )
    try:
        stat = Path(f"/proc/{leftover.pid}/stat").read_text().rpartition(")")[2].split()
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        instance = {
            "instanceId": "vllm-0123456789ab",
            "instanceKey": "deployment:uid-llama:1",
            "pid": leftover.pid,
            "startTime": int(stat[19]),
        }
        content = {
            "version": 1,
            "bootId": boot_id,
            "pidNamespace": os.readlink("/proc/self/ns/pid"),
        }
        record_file.write_text(json.dumps({**content, "instances": [instance]}))

        with TestClient(create_app(db_path=str(tmp_path / "cp.db"))):
            assert leftover.poll() == -signal.SIGTERM
            assert json.loads(record_file.read_text()) == {**content, "instances": []}
    finally:
        leftover.kill()
        leftover.wait()


@pytest.mark.unit
def test_the_controller_deploys_a_service_again_once_its_instance_exits(
    tmp_path, monkeypatch
) -> None:
    """控制器每隔 FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS 秒核对实例：实例在控制器背后停止
    之后，服务以新的实例回到 Ready。"""
    launchers: list[Any] = []
    original = app_module.launcher_from_env

    def recording_launcher_from_env(*args: Any, **kwargs: Any) -> Any:
        launchers.append(original(*args, **kwargs))
        return launchers[-1]

    monkeypatch.setattr(app_module, "launcher_from_env", recording_launcher_from_env)
    monkeypatch.setattr(app_module, "WORKFLOW_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
    monkeypatch.setenv(launcher_config.RUNTIME_LAUNCHER_ENV, "fake")
    monkeypatch.setenv(launcher_config.INSTANCE_CHECK_INTERVAL_ENV, "0.05")

    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        first = _wait_for_instance(client)
        [launcher] = launchers
        client.portal.call(launcher.stop, first["instanceId"])

        second = _wait_for_instance(client, other_than=first["instanceId"])
        assert client.portal.call(launcher.is_running, second["instanceId"])


@pytest.mark.unit
def test_the_controller_reclaims_an_orphaned_allocation_before_it_serves(
    tmp_path, monkeypatch
) -> None:
    """配置启动器时，控制器启动时回收归属资源已不存在的分配：应用开始服务时分配已过期，
    设备空闲。"""
    db_file = tmp_path / "cp.db"
    allocation_id = asyncio.run(_store_orphaned_allocation(db_file))
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
    monkeypatch.setenv(launcher_config.RUNTIME_LAUNCHER_ENV, "fake")

    with TestClient(create_app(db_path=str(db_file))) as client:
        registry = get_registry()
        allocation = client.portal.call(registry.get_allocation, allocation_id)
        assert allocation["status"] == "Expired"
        assert client.portal.call(registry.allocated_devices) == frozenset()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("settings", "with_inventory", "message"),
    [
        pytest.param(
            {"FORGEMESH_RUNTIME_LAUNCHER": "fake"},
            False,
            "FORGEMESH_RUNTIME_LAUNCHER=fake requires FORGEMESH_INVENTORY_FILE",
            id="no-inventory",
        ),
        pytest.param(
            {"FORGEMESH_RUNTIME_LAUNCHER": "docker"},
            True,
            "unsupported FORGEMESH_RUNTIME_LAUNCHER='docker'",
            id="unknown-launcher",
        ),
        pytest.param(
            {"FORGEMESH_RUNTIME_LAUNCHER": "fake", "FORGEMESH_FAKE_LAUNCHER_BIND_HOST": "0.0.0.0"},
            True,
            "FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST must be set",
            id="no-advertise-host",
        ),
        pytest.param(
            {
                "FORGEMESH_RUNTIME_LAUNCHER": "fake",
                "FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS": "0",
            },
            True,
            "FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS='0' must be a positive number of seconds",
            id="instance-check-interval",
        ),
        pytest.param(
            {"FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS": "soon"},
            False,
            "FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS='soon' is not a number of seconds",
            id="instance-check-interval-without-launcher",
        ),
    ],
)
def test_app_refuses_to_start_with_an_unusable_launcher_setting(
    tmp_path, monkeypatch, settings, with_inventory, message
) -> None:
    db_file = tmp_path / "cp.db"
    if with_inventory:
        monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    with (
        pytest.raises(LauncherConfigError, match=message),
        TestClient(create_app(db_path=str(db_file))),
    ):
        pass

    assert not db_file.exists()
    assert not (tmp_path / "cp.workflows.db").exists()


@pytest.mark.unit
@pytest.mark.parametrize("content", [None, '{"nodes": []}'], ids=["missing-file", "invalid-file"])
def test_app_refuses_to_start_with_an_unusable_inventory(tmp_path, monkeypatch, content) -> None:
    db_file = tmp_path / "cp.db"
    inventory = tmp_path / "inventory.json"
    if content is not None:
        inventory.write_text(content)
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(inventory))

    with pytest.raises(InventoryConfigError), TestClient(create_app(db_path=str(db_file))):
        pass

    assert not db_file.exists()


@pytest.mark.unit
def test_failed_controller_start_releases_the_registry_and_authenticator(
    tmp_path, monkeypatch
) -> None:
    async def failing_start(self: ControllerManager) -> None:
        raise RuntimeError("controller failed to start")

    closed: list[SQLiteRegistry] = []
    close = SQLiteRegistry.close

    async def recording_close(self: SQLiteRegistry) -> None:
        closed.append(self)
        await close(self)

    monkeypatch.setattr(ControllerManager, "start", failing_start)
    monkeypatch.setattr(SQLiteRegistry, "close", recording_close)

    with (
        pytest.raises(RuntimeError, match="controller failed to start"),
        TestClient(create_app(db_path=str(tmp_path / "cp.db"))),
    ):
        pass

    with pytest.raises(ComponentNotReadyError):
        get_authenticator()
    with pytest.raises(ComponentNotReadyError):
        get_registry()
    [registry] = closed
    with pytest.raises(sqlite3.ProgrammingError):
        asyncio.run(registry.ping())


def _wait_for_event(
    published: Callable[[], list[dict[str, Any]]],
    event_type: str,
    name: str = "llama",
    timeout: float = 5.0,
) -> dict[str, Any]:
    """轮询直到 ``published()`` 中出现 ``name`` 的 ``event_type`` 事件，返回它的信封。"""
    deadline = time.monotonic() + timeout
    while True:
        for envelope in published():
            if envelope["eventType"] == event_type and envelope["data"]["name"] == name:
                return envelope
        assert time.monotonic() < deadline, f"no {event_type} event was published for {name}"
        time.sleep(0.02)


async def _outbox_event(db_file: Path, event_id: str) -> OutboxEvent | None:
    outbox = Outbox(str(db_file))
    try:
        return outbox.get(event_id)
    finally:
        await outbox.close()


@pytest.mark.unit
def test_the_app_publishes_outbox_events_while_it_runs(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "cp.db"
    monkeypatch.setattr(app_module, "EVENT_POLL_INTERVAL_SECONDS", 0.05)
    received: list[dict[str, Any]] = []
    app = create_app(db_path=str(db_file), event_publisher=lambda e: received.append(e.payload))

    with TestClient(app) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        envelope = _wait_for_event(lambda: list(received), "inference.service.created")

    assert envelope["resourceUid"] == created.json()["metadata"]["uid"]
    published = asyncio.run(_outbox_event(db_file, envelope["eventId"]))
    assert published is not None
    assert published.status == OutboxStatus.PUBLISHED


@pytest.mark.unit
def test_events_stored_while_the_app_was_down_are_published_once(tmp_path, monkeypatch) -> None:
    """启动后发布停机期间写入的事件；已发布的事件在下次启动后不再发布（按创建时间发布，
    它若仍待发布，会先于新事件发出）。"""
    db_file = tmp_path / "cp.db"
    monkeypatch.setattr(app_module, "EVENT_POLL_INTERVAL_SECONDS", 0.05)
    asyncio.run(_store_service(db_file))
    first: list[dict[str, Any]] = []
    second: list[dict[str, Any]] = []

    with TestClient(
        create_app(db_path=str(db_file), event_publisher=lambda e: first.append(e.payload))
    ):
        stored = _wait_for_event(lambda: list(first), "inference.service.created")
    with TestClient(
        create_app(db_path=str(db_file), event_publisher=lambda e: second.append(e.payload))
    ) as client:
        mistral = {**BODY, "metadata": {"name": "mistral", "namespace": "default"}}
        created = client.post("/api/v1/inference-services", json=mistral, headers=HEADERS)
        assert created.status_code == 201
        _wait_for_event(lambda: list(second), "inference.service.created", name="mistral")

    assert stored["resourceUid"] == "uid-llama"
    assert stored["eventId"] not in [envelope["eventId"] for envelope in second]


@pytest.mark.unit
def test_events_are_logged_when_no_webhook_is_configured(tmp_path, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger=EVENT_LOGGER_NAME)
    caplog.set_level(logging.INFO, logger=app_module.__name__)
    monkeypatch.setattr(app_module, "EVENT_POLL_INTERVAL_SECONDS", 0.05)

    def logged() -> list[dict[str, Any]]:
        records = [r for r in list(caplog.records) if r.name == EVENT_LOGGER_NAME]
        return [json.loads(record.getMessage()) for record in records]

    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        envelope = _wait_for_event(logged, "inference.service.created")

    assert envelope["tenantId"] == "acme"
    assert f"outbox events are published with LogPublisher(logger={EVENT_LOGGER_NAME!r})" in (
        caplog.messages
    )


@pytest.mark.unit
@pytest.mark.parametrize("launcher", [None, "fake"], ids=["no-launcher", "fake-launcher"])
def test_the_dispatcher_runs_from_before_recovery_until_the_registry_closes(
    tmp_path, monkeypatch, launcher
) -> None:
    """Dispatcher 在恢复部署、核对实例之前启动；关闭时在控制器（含定期核对实例与回收分配的
    任务）、工作流运行时与启动器之后，关闭数据库之前停止。"""
    calls: list[str] = []

    def record(owner: Any, name: str, label: str) -> None:
        original = getattr(owner, name)

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls.append(label)
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    start = Dispatcher.start

    def recording_start(self: Dispatcher) -> asyncio.Task[None]:
        calls.append("dispatcher.start")
        return start(self)

    monkeypatch.setattr(Dispatcher, "start", recording_start)
    record(FakeLauncher, "start", "launcher.start")
    record(LocalWorkflowRuntime, "recover", "runtime.recover")
    record(app_module, "check_instances", "check_instances")
    record(ControllerManager, "start", "controller.start")
    record(ControllerManager, "stop", "controller.stop")
    record(LocalWorkflowRuntime, "aclose", "runtime.aclose")
    record(FakeLauncher, "aclose", "launcher.aclose")
    record(Dispatcher, "shutdown", "dispatcher.shutdown")
    record(SQLiteRegistry, "close", "registry.close")
    if launcher is not None:
        monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
        monkeypatch.setenv(launcher_config.RUNTIME_LAUNCHER_ENV, launcher)

    with TestClient(create_app(db_path=str(tmp_path / "cp.db"))):
        started = list(calls)

    recovery = ["launcher.start", "runtime.recover", "check_instances"] if launcher else []
    released = ["runtime.aclose", "launcher.aclose"] if launcher else []
    assert started == ["dispatcher.start", *recovery, "controller.start"]
    assert calls[len(started) :] == [
        "controller.stop",
        *released,
        "dispatcher.shutdown",
        "registry.close",
    ]


@pytest.mark.unit
def test_the_degraded_status_written_at_startup_is_published(tmp_path, monkeypatch) -> None:
    """启动时核对实例写下的事件同样发布：上次运行的实例没能活过重启，服务转为 ``Degraded``，
    随后以新的实例回到 Ready。"""
    db_file = tmp_path / "cp.db"
    monkeypatch.setattr(app_module, "EVENT_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "WORKFLOW_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(_write_inventory(tmp_path)))
    monkeypatch.setenv(launcher_config.RUNTIME_LAUNCHER_ENV, "fake")
    with TestClient(create_app(db_path=str(db_file), event_publisher=lambda e: None)) as client:
        created = client.post("/api/v1/inference-services", json=BODY, headers=HEADERS)
        assert created.status_code == 201
        first = _wait_for_instance(client)
    received: list[dict[str, Any]] = []

    with TestClient(
        create_app(db_path=str(db_file), event_publisher=lambda e: received.append(e.payload))
    ) as client:
        _wait_for_instance(client, other_than=first["instanceId"])
        degraded = _wait_for_event(
            lambda: [e for e in list(received) if e["data"]["phase"] == "Degraded"],
            "inference.service.status_updated",
        )

    conditions = {c["type"]: c for c in degraded["data"]["resource"]["status"]["conditions"]}
    assert conditions["Ready"]["reason"] == "InstanceLost"
    assert "did not survive the control plane restart" in conditions["Ready"]["message"]
    [endpoint] = degraded["data"]["resource"]["status"]["endpoints"]
    assert (endpoint["instanceId"], endpoint["ready"]) == (first["instanceId"], False)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param(
            {"FORGEMESH_EVENT_WEBHOOK_URL": "https://user:s3cret@hooks.example.com/"},
            "FORGEMESH_EVENT_WEBHOOK_URL must not contain user information",
            id="credentials-in-url",
        ),
        pytest.param(
            {"FORGEMESH_EVENT_WEBHOOK_TOKEN": "s3cret"},
            "FORGEMESH_EVENT_WEBHOOK_TOKEN only applies to the webhook publisher",
            id="token-without-url",
        ),
        pytest.param(
            {
                "FORGEMESH_EVENT_WEBHOOK_URL": "https://hooks.example.com/",
                "FORGEMESH_EVENT_WEBHOOK_TIMEOUT_SECONDS": "0",
            },
            "FORGEMESH_EVENT_WEBHOOK_TIMEOUT_SECONDS='0' must be a number of seconds",
            id="zero-timeout",
        ),
    ],
)
def test_app_refuses_to_start_with_an_invalid_event_setting(
    tmp_path, monkeypatch, settings, message
) -> None:
    db_file = tmp_path / "cp.db"
    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    with (
        pytest.raises(EventConfigError, match=message) as excinfo,
        TestClient(create_app(db_path=str(db_file))),
    ):
        pass

    assert "s3cret" not in str(excinfo.value)
    assert not db_file.exists()
