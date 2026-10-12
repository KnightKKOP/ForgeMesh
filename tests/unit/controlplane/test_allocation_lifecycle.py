"""分配生命周期测试：控制器 + 协调器 + 首次适配调度器 + 注册表。

* 同一 generation 的重放（含分配后、status 写入前崩溃，再以文件库重启）始终只有
  一个分配，分配的设备满足 ``spec.resources.accelerator``，部署工作流拿到的就是它；
* 持有分配、还没有实例的资源（Preparing）被删除后不留下 Active 分配，同名资源重建后
  能再次分到同一设备。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from forgemesh.controlplane.controller import ControllerManager
from forgemesh.controlplane.deployment import LaunchRequest
from forgemesh.controlplane.reconciler import ProcessedKeyStore, ReconciliationController
from forgemesh.controlplane.registry import ResourceKey, SQLiteRegistry
from forgemesh.controlplane.scheduler import FirstFitScheduler, Inventory
from forgemesh.core.errors import NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.scheduler import AllocatedDevice
from forgemesh.policy.engine import DefaultPolicyEngine
from forgemesh.policy.rules import AuthzRule
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferenceService,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)
from forgemesh.workflow.runtime import RunRef

KIND = "InferenceService"
TENANT = "tenant-a"
NAMESPACE = "team-a"
KEY = ResourceKey(TENANT, KIND, NAMESPACE, "svc")


def _inventory(*devices: tuple[str, int]) -> Inventory:
    """单节点 ``node-a`` 的清单，设备按 ``(设备 id, 显存 GiB)`` 给出。"""
    node = {
        "id": "node-a",
        "devices": [
            {"id": device_id, "type": "nvidia-gpu", "memoryGiB": memory}
            for device_id, memory in devices
        ],
    }
    return Inventory.model_validate_json(json.dumps({"nodes": [node]}))


def _service(uid: str, min_memory: int = 16) -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(
            name="svc", namespace=NAMESPACE, uid=uid, tenant_id=TENANT, generation=1
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=min_memory)
            ),
        ),
    )


class _Workflow:
    """只记录启动请求的部署工作流替身：本模块的部署停在 Deploying 之前。"""

    def __init__(self) -> None:
        self.requests: list[LaunchRequest] = []

    async def start(self, defn: Any, inputs: dict[str, Any], idempotency_key: str) -> RunRef:
        self.requests.append(LaunchRequest.model_validate(inputs["request"]))
        return RunRef(run_id=idempotency_key, definition_version=1)


class _Release:
    async def release(self, resource: Resource) -> None:
        return None


def _manager(
    registry: SQLiteRegistry,
    inventory: Inventory,
    store: ProcessedKeyStore | None = None,
    workflow: _Workflow | None = None,
) -> ControllerManager:
    reconciler = ReconciliationController(
        registry,
        DefaultPolicyEngine(authz_rules=[AuthzRule(action="reconcile", effect="allow")]),
        scheduler=FirstFitScheduler(registry, inventory),
        workflow_runtime=workflow or _Workflow(),
        release_provider=_Release(),
        processed_store=store,
        record_dedup_keys=False,
    )
    return ControllerManager(registry, reconciler)


@pytest.mark.unit
def test_replaying_a_generation_after_a_crash_and_restart_keeps_one_allocation(
    tmp_path, monkeypatch
):
    """Scheduling 一轮分配之后、status 写入之前进程退出：重启后同一 generation 再次分配，
    拿到的仍是原来的分配，部署工作流收到的也是它的设备。"""
    db = str(tmp_path / "cp.db")
    inventory = _inventory(("gpu-0", 24), ("gpu-1", 80))
    workflow = _Workflow()

    async def killed(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("process killed")

    async def before_crash() -> list[dict[str, Any]]:
        registry = SQLiteRegistry(db)
        store = ProcessedKeyStore(db)
        try:
            await registry.put(_service("uid-1", min_memory=40))
            manager = _manager(registry, inventory, store)
            await manager.process(KEY)
            monkeypatch.setattr(registry, "update_status", killed)
            with pytest.raises(RuntimeError, match="process killed"):
                await manager.process(KEY)
            return await registry.list_allocations(TENANT)
        finally:
            await store.close()
            await registry.close()

    async def after_restart() -> tuple[InferenceService, list[dict[str, Any]]]:
        registry = SQLiteRegistry(db)
        store = ProcessedKeyStore(db)
        try:
            manager = _manager(registry, inventory, store, workflow)
            await manager.process(KEY)
            await manager.process(KEY)
            service = await registry.get(KIND, NAMESPACE, "svc", TENANT)
            return service, await registry.list_allocations(TENANT)
        finally:
            await store.close()
            await registry.close()

    [allocation] = asyncio.run(before_crash())
    service, allocations = asyncio.run(after_restart())

    assert allocations == [allocation]
    assert (allocation["resource_id"], allocation["idempotency_key"]) == ("uid-1", "uid-1:1")
    assert allocation["devices"] == [{"node_id": "node-a", "device_id": "gpu-1"}]
    assert service.status.phase == "Deploying"
    [request] = workflow.requests
    assert request.allocation_id == allocation["id"]
    assert request.devices == (AllocatedDevice(node_id="node-a", device_id="gpu-1"),)


@pytest.mark.unit
def test_deleting_an_allocated_resource_frees_its_device_for_a_re_created_one():
    """单卡清单：Preparing 不是有外部副作用的 phase（还没有实例），但资源持有分配，删除
    仍先进入 Terminating；协调器释放分配并物理删除后，同名资源以新 uid 重建能分到这张卡。"""
    inventory = _inventory(("gpu-0", 80))

    async def scenario() -> None:
        registry = SQLiteRegistry()
        try:
            manager = _manager(registry, inventory)
            await registry.put(_service("uid-1"))
            await manager.process(KEY)
            await manager.process(KEY)
            assert (await registry.get(KIND, NAMESPACE, "svc", TENANT)).status.phase == "Preparing"
            [first] = await registry.list_allocations(TENANT)

            deletion = await registry.request_delete(KIND, NAMESPACE, "svc", TENANT)
            assert deletion.deleted is False
            await manager.process(KEY)

            with pytest.raises(NotFoundError):
                await registry.get(KIND, NAMESPACE, "svc", TENANT)
            assert await registry.get_active_allocation("uid-1", TENANT) is None
            assert await registry.allocated_devices() == frozenset()
            assert (await registry.get_allocation(first["id"]))["status"] == "Released"

            await registry.put(_service("uid-2"))
            await manager.process(KEY)
            await manager.process(KEY)

            second = await registry.get_active_allocation("uid-2", TENANT)
            assert second is not None and second["id"] != first["id"]
            assert (
                second["devices"]
                == first["devices"]
                == [{"node_id": "node-a", "device_id": "gpu-0"}]
            )
        finally:
            await registry.close()

    asyncio.run(scenario())
