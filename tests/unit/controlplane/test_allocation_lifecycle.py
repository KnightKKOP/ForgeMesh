"""分配生命周期测试：控制器 + 协调器 + 首次适配调度器 + 注册表。

* 同一 generation 的重放（含分配后、status 写入前崩溃，再以文件库重启）始终只有
  一个分配，且分配的设备满足 ``spec.resources.accelerator``；
* 处于 Scheduling 的资源被删除后不留下 Active 分配，同名资源重建后能再次分到同一设备。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from forgemesh.controlplane.controller import ControllerManager
from forgemesh.controlplane.reconciler import ProcessedKeyStore, ReconciliationController
from forgemesh.controlplane.registry import ResourceKey, SQLiteRegistry
from forgemesh.controlplane.scheduler import FirstFitScheduler, Inventory
from forgemesh.core.errors import NotFoundError
from forgemesh.core.objects import ObjectMeta
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
    async def start(self, defn: Any, inputs: Any, idempotency_key: str) -> RunRef:
        return RunRef(run_id=idempotency_key, definition_version=1)


def _manager(
    registry: SQLiteRegistry, inventory: Inventory, store: ProcessedKeyStore | None = None
) -> ControllerManager:
    reconciler = ReconciliationController(
        registry,
        DefaultPolicyEngine(authz_rules=[AuthzRule(action="reconcile", effect="allow")]),
        scheduler=FirstFitScheduler(registry, inventory),
        workflow_runtime=_Workflow(),
        processed_store=store,
        record_dedup_keys=False,
    )
    return ControllerManager(registry, reconciler)


@pytest.mark.unit
def test_replaying_a_generation_after_a_crash_and_restart_keeps_one_allocation(
    tmp_path, monkeypatch
):
    """分配之后、status 写入之前进程退出：去重键没有登记，重启后同一 generation 再次
    协调，拿到的仍是原来的分配。"""
    db = str(tmp_path / "cp.db")
    inventory = _inventory(("gpu-0", 24), ("gpu-1", 80))

    async def killed(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("process killed")

    async def before_crash() -> list[dict[str, Any]]:
        registry = SQLiteRegistry(db)
        store = ProcessedKeyStore(db)
        try:
            await registry.put(_service("uid-1", min_memory=40))
            monkeypatch.setattr(registry, "update_status", killed)
            with pytest.raises(RuntimeError, match="process killed"):
                await _manager(registry, inventory, store).process(KEY)
            return await registry.list_allocations(TENANT)
        finally:
            await store.close()
            await registry.close()

    async def after_restart() -> tuple[InferenceService, list[dict[str, Any]]]:
        registry = SQLiteRegistry(db)
        store = ProcessedKeyStore(db)
        try:
            manager = _manager(registry, inventory, store)
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
    assert [condition.reason for condition in service.status.conditions] == ["Reconciled"]


@pytest.mark.unit
def test_deleting_a_scheduling_resource_frees_its_device_for_a_re_created_one():
    """单卡清单：Scheduling 不是有外部副作用的 phase，但资源持有分配，删除仍先进入
    Terminating；协调器释放分配并物理删除后，同名资源以新 uid 重建能分到这张卡。"""
    inventory = _inventory(("gpu-0", 80))

    async def scenario() -> None:
        registry = SQLiteRegistry()
        try:
            manager = _manager(registry, inventory)
            await registry.put(_service("uid-1"))
            await manager.process(KEY)
            [first] = await registry.list_allocations(TENANT)
            await registry.update_status(KIND, NAMESPACE, "svc", TENANT, {"phase": "Scheduling"})

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
