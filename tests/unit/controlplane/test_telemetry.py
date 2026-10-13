"""控制平面的指标：协调结果与耗时、重试、部署结果、Outbox 的发布尝试，以及抓取时读取的状态类指标。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from forgemesh.controlplane.controller import ControllerManager, WorkQueue
from forgemesh.controlplane.metrics import MetricsRegistry
from forgemesh.controlplane.outbox import PublishOutcome, RetryPolicy
from forgemesh.controlplane.registry import ResourceKey, SQLiteRegistry
from forgemesh.controlplane.scheduler import Inventory
from forgemesh.controlplane.telemetry import ControllerMetrics, ControlPlaneMetrics
from forgemesh.core.objects import ObjectMeta
from forgemesh.core.reconciler import ReconcileResult
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
from forgemesh.workflow.runtime import Step, WorkflowDefinition
from forgemesh.workflow.states import WorkflowStatus
from forgemesh.workflowruntime.store import WorkflowStore

from .exposition import parse

KIND = "InferenceService"
TENANT = "tenant-a"
NAMESPACE = "ns-hidden"
INVENTORY = Inventory.model_validate_json(
    json.dumps(
        {
            "nodes": [
                {
                    "id": "node-a",
                    "devices": [
                        {"id": "gpu-0", "type": "nvidia-gpu", "memoryGiB": 80},
                        {"id": "gpu-1", "type": "nvidia-gpu", "memoryGiB": 80},
                    ],
                },
                {
                    "id": "node-b",
                    "devices": [{"id": "npu-0", "type": "ascend-npu", "memoryGiB": 64}],
                },
            ]
        }
    )
)


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _service(
    name: str, tenant: str = TENANT, phase: InferencePhase = InferencePhase.PENDING
) -> InferenceService:
    return InferenceService(
        metadata=ObjectMeta(name=name, namespace=NAMESPACE, tenant_id=tenant, uid=f"uid-{name}"),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=phase),
    )


def _key(name: str) -> ResourceKey:
    return ResourceKey(TENANT, KIND, NAMESPACE, name)


def _ready(status: str, reason: str) -> dict[str, str]:
    return {"type": "Ready", "status": status, "reason": reason, "message": "details"}


class StubReconciler:
    """依次返回给定的结果：异常则抛出，异步函数则返回它的结果。"""

    records_dedup_keys = False

    def __init__(
        self, *results: ReconcileResult | Exception | Callable[[], Awaitable[ReconcileResult]]
    ) -> None:
        self.results = list(results)

    async def reconcile(self, ctx: Any) -> ReconcileResult:
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result()
        return result

    async def mark_processed(self, dedup_key: str) -> None:
        return None


class FailingReconciler(StubReconciler):
    async def reconcile(self, ctx: Any) -> ReconcileResult:
        raise RuntimeError("reconciler backend unavailable")


async def _eventually(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition was not met in time"
        await asyncio.sleep(0.01)


@pytest.mark.unit
def test_reconcile_outcomes_and_durations_are_counted_per_operation() -> None:
    async def scenario() -> str:
        registry = SQLiteRegistry()

        async def deleted_meanwhile() -> ReconcileResult:
            await registry.delete(KIND, NAMESPACE, "svc", TENANT)
            return ReconcileResult(status_patch={"conditions": [_ready("False", "Waiting")]})

        reconciler = StubReconciler(
            ReconcileResult(requeue_after=30),
            ReconcileResult(),
            ReconcileResult(done=True),
            RuntimeError("boom"),
            deleted_meanwhile,
            ReconcileResult(done=True),
        )
        metrics = MetricsRegistry()
        manager = ControllerManager(registry, reconciler, metrics=ControllerMetrics(metrics))
        await registry.put(_service("svc"))
        for _ in range(3):
            await manager.process(_key("svc"))
        with pytest.raises(RuntimeError):
            await manager.process(_key("svc"))
        await manager.process(_key("svc"))
        await registry.put(_service("old", phase=InferencePhase.READY))
        await registry.request_delete(KIND, NAMESPACE, "old", TENANT)
        await manager.process(_key("old"))
        await manager.process(_key("missing"))
        await registry.close()
        return metrics.render()

    families = parse(_run(scenario()))
    reconciles = families["forgemesh_reconcile_total"]
    assert {(s.labels["operation"], s.labels["outcome"]): s.value for s in reconciles.samples} == {
        ("reconcile", "requeue"): 1,
        ("reconcile", "wait"): 1,
        ("reconcile", "done"): 1,
        ("reconcile", "error"): 1,
        ("reconcile", "conflict"): 1,
        ("terminate", "done"): 1,
    }
    duration = families["forgemesh_reconcile_duration_seconds"]
    count = "forgemesh_reconcile_duration_seconds_count"
    assert duration.value(count, operation="reconcile") == 5
    assert duration.value(count, operation="terminate") == 1


@pytest.mark.unit
def test_reconciles_that_raise_are_counted_as_retries() -> None:
    async def scenario() -> tuple[str, int]:
        registry = SQLiteRegistry()
        queue: WorkQueue[ResourceKey] = WorkQueue(base_delay=0.01, max_delay=0.05, jitter=0)
        metrics = MetricsRegistry()
        manager = ControllerManager(
            registry,
            FailingReconciler(),
            queue=queue,
            poll_interval=0.01,
            metrics=ControllerMetrics(metrics),
        )
        await registry.put(_service("svc"))
        await manager.start()
        try:
            await _eventually(lambda: queue.failures(_key("svc")) >= 2)
        finally:
            await manager.stop()
            await registry.close()
        return metrics.render(), queue.failures(_key("svc"))

    text, failures = _run(scenario())
    families = parse(text)
    assert families["forgemesh_controller_retries_total"].value() == failures
    errors = families["forgemesh_reconcile_total"].value(operation="reconcile", outcome="error")
    assert errors == failures


@pytest.mark.unit
def test_deployments_count_transitions_into_ready_or_failed_with_bounded_reasons() -> None:
    registry = MetricsRegistry()
    metrics = ControllerMetrics(registry)
    ready = {"phase": "Ready", "conditions": [_ready("True", "Reconciled")]}

    metrics.status_written("Verifying", ready)
    metrics.status_written("Ready", ready)
    metrics.status_written(
        "Deploying", {"phase": "Failed", "conditions": [_ready("False", "WorkflowFailed")]}
    )
    metrics.status_written(
        "Verifying", {"phase": "Failed", "conditions": [_ready("False", "run 7f3a failed")]}
    )
    metrics.status_written("Pending", {"phase": "Failed"})
    metrics.status_written(
        "Ready", {"phase": "Degraded", "conditions": [_ready("False", "InstanceLost")]}
    )
    metrics.status_written("Pending", {"conditions": [_ready("False", "SchedulerUnavailable")]})

    deployments = parse(registry.render())["forgemesh_deployments_total"]
    assert {(s.labels["outcome"], s.labels["reason"]): s.value for s in deployments.samples} == {
        ("ready", "Reconciled"): 1,
        ("failed", "WorkflowFailed"): 1,
        ("failed", "Unknown"): 2,
    }


@pytest.mark.unit
def test_publish_attempts_are_counted_by_outcome_starting_from_zero() -> None:
    metrics = ControlPlaneMetrics()
    name = "forgemesh_outbox_publish_attempts_total"
    initial = parse(metrics.registry.render())[name]
    for outcome in (PublishOutcome.PUBLISHED, PublishOutcome.PUBLISHED, PublishOutcome.FAILED):
        metrics.outbox.attempted(outcome)
    attempts = parse(metrics.registry.render())[name]

    assert initial.type == "counter"
    assert {s.labels["outcome"]: s.value for s in initial.samples} == {
        "published": 0,
        "failed": 0,
        "dead": 0,
    }
    assert {s.labels["outcome"]: s.value for s in attempts.samples} == {
        "published": 2,
        "failed": 1,
        "dead": 0,
    }


@pytest.mark.unit
def test_state_metrics_are_read_at_scrape_time_without_tenant_data() -> None:
    definition = WorkflowDefinition(name="deploy", steps=[Step(id="launch", uses="launch")])

    async def scenario() -> tuple[str, str]:
        registry = SQLiteRegistry()
        store = WorkflowStore()
        metrics = ControlPlaneMetrics()
        manager = ControllerManager(registry, StubReconciler(), metrics=metrics.controller)
        services = {
            "svc-pending": ("tenant-a", InferencePhase.PENDING, None),
            "svc-gpu": ("tenant-b", InferencePhase.READY, [("node-a", "gpu-1")]),
            "svc-lost": ("tenant-b", InferencePhase.READY, [("node-gone", "gpu-9")]),
            "svc-cpu": ("tenant-c", InferencePhase.DEPLOYING, []),
            "svc-released": ("tenant-c", InferencePhase.FAILED, [("node-a", "gpu-0")]),
        }
        for name, (tenant, phase, devices) in services.items():
            await registry.put(_service(name, tenant, phase))
            if devices is None:
                continue
            record = await registry.create_allocation(
                tenant,
                ResourceRef(kind=KIND, namespace=NAMESPACE, name=name, uid=f"uid-{name}"),
                f"{name}:1",
                [AllocatedDevice(node_id=node, device_id=device) for node, device in devices],
            )
            if name == "svc-released":
                await registry.release_allocation(record["id"], tenant)
        running = await store.create_run(definition, {}, "run-1")
        await store.create_run(definition, {}, "run-2")
        await store.update_run_status(running.run_id, WorkflowStatus.RUNNING.value)
        await manager.resync()

        unwatch = metrics.watch(registry, manager, INVENTORY, store)
        watched = await metrics.registry.scrape()
        unwatch()
        await registry.ping()
        unwatched = await metrics.registry.scrape()
        await store.close()
        await registry.close()
        return watched, unwatched

    watched, unwatched = _run(scenario())
    families = parse(watched)
    services = families["forgemesh_inference_services"]
    assert {s.labels["phase"]: s.value for s in services.samples} == {
        **dict.fromkeys((phase.value for phase in InferencePhase), 0),
        "Pending": 1,
        "Ready": 2,
        "Deploying": 1,
        "Failed": 1,
    }
    allocations = families["forgemesh_allocations_active"]
    assert {s.labels["accelerator_type"]: s.value for s in allocations.samples} == {
        "nvidia-gpu": 1,
        "ascend-npu": 0,
        "unknown": 1,
        "none": 1,
    }
    devices = families["forgemesh_accelerator_devices"]
    assert {
        (s.labels["accelerator_type"], s.labels["state"]): s.value for s in devices.samples
    } == {
        ("nvidia-gpu", "allocated"): 1,
        ("nvidia-gpu", "free"): 1,
        ("ascend-npu", "allocated"): 0,
        ("ascend-npu", "free"): 1,
    }
    runs = families["forgemesh_workflow_runs"]
    assert {s.labels["status"]: s.value for s in runs.samples} == {
        **dict.fromkeys((status.value for status in WorkflowStatus), 0),
        "Pending": 1,
        "Running": 1,
    }
    depth = families["forgemesh_controller_queue_depth"]
    assert {s.labels["state"]: s.value for s in depth.samples} == {
        "ready": 5,
        "delayed": 0,
        "processing": 0,
    }
    events = families["forgemesh_outbox_events"]
    assert {s.labels["state"]: s.value for s in events.samples} == {
        "pending": 5,
        "retrying": 0,
        "dead": 0,
    }
    lock_wait_count = "forgemesh_database_lock_wait_seconds_count"
    assert families["forgemesh_database_lock_wait_seconds"].value(lock_wait_count) == 3
    for hidden in ("tenant-", NAMESPACE, "svc-", "uid-", "node-", "gpu-", "run-"):
        assert hidden not in watched

    after = parse(unwatched)
    for name in (
        "forgemesh_inference_services",
        "forgemesh_allocations_active",
        "forgemesh_accelerator_devices",
        "forgemesh_workflow_runs",
        "forgemesh_controller_queue_depth",
        "forgemesh_outbox_events",
    ):
        assert after[name].samples == []
    assert after["forgemesh_database_lock_wait_seconds"].value(lock_wait_count) == 3


@pytest.mark.unit
def test_state_metrics_from_an_unavailable_registry_are_dropped(caplog) -> None:
    async def scenario() -> tuple[str, str]:
        registry = SQLiteRegistry()
        metrics = ControlPlaneMetrics()
        manager = ControllerManager(registry, StubReconciler(), metrics=metrics.controller)
        await registry.put(_service("svc"))
        metrics.watch(registry, manager)
        available = await metrics.registry.scrape()
        await registry.close()
        return available, await metrics.registry.scrape()

    with caplog.at_level(logging.WARNING, logger="forgemesh.controlplane.metrics"):
        available, unavailable = _run(scenario())

    assert parse(available)["forgemesh_inference_services"].value(phase="Pending") == 1
    assert parse(available)["forgemesh_outbox_events"].value(state="pending") == 1
    families = parse(unavailable)
    assert families["forgemesh_inference_services"].samples == []
    assert families["forgemesh_allocations_active"].samples == []
    assert families["forgemesh_workflow_runs"].samples == []
    assert families["forgemesh_outbox_events"].samples == []
    assert families["forgemesh_controller_queue_depth"].value(state="ready") == 0
    assert "metrics collector" in caplog.text


@pytest.mark.unit
def test_the_outbox_backlog_is_read_by_state_at_scrape_time() -> None:
    async def scenario() -> tuple[str, str]:
        registry = SQLiteRegistry()
        metrics = ControlPlaneMetrics()
        manager = ControllerManager(registry, StubReconciler(), metrics=metrics.controller)
        for name in ("svc-a", "svc-b", "svc-c"):
            await registry.put(_service(name))
        unwatch = metrics.watch(registry, manager)
        before = await metrics.registry.scrape()
        retried, dead, _ = await asyncio.to_thread(registry.outbox.get_pending)
        await asyncio.to_thread(registry.outbox.mark_failed, retried.event_id, "refused")
        await asyncio.to_thread(
            registry.outbox.mark_failed,
            dead.event_id,
            "refused",
            policy=RetryPolicy(max_attempts=1),
        )
        after = await metrics.registry.scrape()
        unwatch()
        await registry.close()
        return before, after

    before, after = _run(scenario())
    initial = parse(before)["forgemesh_outbox_events"]
    events = parse(after)["forgemesh_outbox_events"]
    assert initial.type == "gauge"
    assert {s.labels["state"]: s.value for s in initial.samples} == {
        "pending": 3,
        "retrying": 0,
        "dead": 0,
    }
    assert {s.labels["state"]: s.value for s in events.samples} == {
        "pending": 1,
        "retrying": 1,
        "dead": 1,
    }


@pytest.mark.unit
def test_lock_wait_covers_every_store_sharing_the_registry_database() -> None:
    async def scenario() -> tuple[str, str]:
        registry = SQLiteRegistry()
        metrics = ControlPlaneMetrics()
        manager = ControllerManager(registry, StubReconciler(), metrics=metrics.controller)
        unwatch = metrics.watch(registry, manager)
        await registry.ping()
        await asyncio.to_thread(registry.outbox.get_stats)
        await asyncio.to_thread(registry.audit_log.list_page, TENANT)
        watched = metrics.registry.render()
        unwatch()
        await registry.ping()
        await asyncio.to_thread(registry.outbox.get_stats)
        unwatched = metrics.registry.render()
        await registry.close()
        return watched, unwatched

    watched, unwatched = _run(scenario())
    count = "forgemesh_database_lock_wait_seconds_count"
    assert parse(watched)["forgemesh_database_lock_wait_seconds"].value(count) == 3
    assert parse(unwatched)["forgemesh_database_lock_wait_seconds"].value(count) == 3
