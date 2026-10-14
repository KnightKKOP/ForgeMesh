"""控制器管理器：发现资源变更，按资源排队，驱动协调器并回写 status。

数据流::

    resource_changes ──轮询──▶ WorkQueue ──worker──▶ ReconciliationController
           ▲                                                  │ status_patch
           └────────── SQLiteRegistry.update_status ◀─────────┘

* **变更来源**：启动时先记下当前变更序号，再全量列出资源键重建队列，之后按
  序号增量轮询 ``resource_changes`` 并清理已读记录，另有周期性全量同步兜底。
  ``status`` 变更（包括控制器自己的写入）不触发协调。
* **实例核对**：注入 ``instance_check`` 时每隔 ``instance_check_interval`` 秒调用一次，把它
  报告的资源加入队列（应用注入绑定了注册表与启动器的
  :func:`~forgemesh.controlplane.deployment.check_instances`：实例已不在运行的 ``Ready``
  服务由它转为 ``Degraded``，实例回来了的 ``Degraded`` 服务由它报告）；核对失败时记日志，
  下一个周期重试。
* **回收分配**：注入 ``reclaim`` 时，启动时与每次周期性全量同步之后各调用一次（应用注入
  绑定了注册表与 :class:`~forgemesh.controlplane.deployment.DeploymentReleaser` 的
  :func:`~forgemesh.controlplane.deployment.reclaim_orphaned_allocations`：停止归属资源已
  不存在的分配所属的部署，再让分配过期）；失败时记日志，下一次全量同步之后重试，启动照常
  继续。
* **并发**：``workers`` 个 worker 协程；同一资源同一时刻只由一个 worker 处理。
* **status 回写**：以协调前读到的 ``resource_version`` 做 CAS。冲突时重读：
  ``uid``、``generation`` 与操作（reconcile / recover / reclaim / terminate）都没变就以新
  版本重试，否则丢弃这次结果并立即重新排队。
* **去重键**：协调器以 ``record_dedup_keys=False`` 构造，管理器在 status 写入
  成功之后才登记去重键，避免"已登记、状态却没写进去"。
* **重新排队**：``requeue_after`` 非空时按该延迟重试；为空时不再定时重试，等待
  资源变更或下一次全量同步；协调抛出异常时按资源指数退避。
* **单写者**（ADR-013）：控制器随 API 进程运行，不得在多个进程或副本中同时运行。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from typing import Any
from uuid import uuid4

from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import Resource
from forgemesh.core.reconciler import ReconcileContext

from ..deployment import DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS
from ..reconciler import ReconciliationController, compute_dedup_key
from ..registry import ChangeType, ResourceKey, SQLiteRegistry
from ..telemetry import ControllerMetrics, InstanceCheckOutcome, ReconcileOutcome
from .queue import WorkQueue

logger = logging.getLogger(__name__)

#: 默认协调的资源 kind。
DEFAULT_KINDS: tuple[str, ...] = ("InferenceService",)

#: 读取变更记录连续失败时的最长退避（秒）。
MAX_POLL_BACKOFF_SECONDS = 30.0

_TERMINATING = "Terminating"
_DEGRADED = "Degraded"
_FAILED = "Failed"


def _operation(phase: str | None) -> str:
    """去重键与 status 写入核对所用的操作。

    ``Degraded`` 单独使用 ``recover``：同一代 spec 可能多次丢失实例、多次恢复，部署进入
    ``Ready`` 时登记的去重键不得挡住恢复；报告实例丢失（``Ready`` 转为 ``Degraded``）也让
    读到 ``Ready`` 的那一轮的结果作废，不会覆盖这次报告。``Failed`` 单独使用 ``reclaim``：
    进入 ``Failed`` 的一轮登记的去重键不得挡住之后停止遗留实例、释放分配的一轮。
    """
    if phase == _TERMINATING:
        return "terminate"
    if phase == _DEGRADED:
        return "recover"
    if phase == _FAILED:
        return "reclaim"
    return "reconcile"


def _describe(key: ResourceKey) -> str:
    return f"{key.kind} {key.tenant_id}/{key.namespace or ''}/{key.name}"


class ControllerManager:
    """驱动 :class:`ReconciliationController` 的控制器管理器。

    Args:
        registry: 权威注册中心。
        reconciler: 以 ``record_dedup_keys=False`` 构造的协调器。
        kinds: 要协调的资源 kind。
        workers: worker 协程数。
        poll_interval: 轮询变更记录的间隔（秒），即发现变更的最大延迟。
        resync_period: 全量同步周期（秒）。
        batch_size: 每次读取的变更记录条数上限。
        status_write_attempts: status 写入遇到版本冲突时的最多尝试次数。
        instance_check: 可选，核对已部署实例的协程函数，返回要协调的资源键。
        instance_check_interval: 两次核对实例之间的秒数。
        reclaim: 可选，回收孤立分配的协程函数（返回值不使用）。
        queue: 工作队列；为空时新建（测试可注入）。
        metrics: 协调、队列与定期核对实例的指标；为空时新建（不会被导出）。
    """

    def __init__(
        self,
        registry: SQLiteRegistry,
        reconciler: ReconciliationController,
        *,
        kinds: Collection[str] = DEFAULT_KINDS,
        workers: int = 2,
        poll_interval: float = 0.5,
        resync_period: float = 600.0,
        batch_size: int = 500,
        status_write_attempts: int = 3,
        instance_check: Callable[[], Awaitable[Iterable[ResourceKey]]] | None = None,
        instance_check_interval: float = DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS,
        reclaim: Callable[[], Awaitable[object]] | None = None,
        queue: WorkQueue[ResourceKey] | None = None,
        metrics: ControllerMetrics | None = None,
    ) -> None:
        if reconciler.records_dedup_keys:
            raise ValueError(
                "the reconciler must be built with record_dedup_keys=False so that dedup keys "
                "are recorded only after the status has been written"
            )
        if workers < 1 or batch_size < 1 or status_write_attempts < 1:
            raise ValueError("workers, batch_size and status_write_attempts must be >= 1")
        if poll_interval <= 0 or resync_period <= 0 or instance_check_interval <= 0:
            raise ValueError("poll_interval, resync_period and instance_check_interval must be > 0")
        self._registry = registry
        self._reconciler = reconciler
        self._kinds = frozenset(kinds)
        self._worker_count = workers
        self._poll_interval = poll_interval
        self._resync_period = resync_period
        self._batch_size = batch_size
        self._status_write_attempts = status_write_attempts
        self._instance_check = instance_check
        self._instance_check_interval = instance_check_interval
        self._reclaim = reclaim
        self._queue: WorkQueue[ResourceKey] = queue if queue is not None else WorkQueue()
        self._metrics = metrics if metrics is not None else ControllerMetrics()
        self._cursor = 0
        self._started = False
        self._watchers: list[asyncio.Task[None]] = []
        self._workers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """重建工作队列并回收一次孤立的分配，然后启动变更轮询、全量同步、实例核对与 worker
        任务。"""
        if self._started:
            raise RuntimeError("a controller manager can only be started once")
        self._started = True
        self._cursor = await self._registry.latest_change_seq()
        await self.resync()
        await self._reclaim_logging_failures()
        self._watchers = [
            asyncio.create_task(self._watch_changes(), name="forgemesh-controller-changes"),
            asyncio.create_task(self._resync_periodically(), name="forgemesh-controller-resync"),
        ]
        if self._instance_check is not None:
            self._watchers.append(
                asyncio.create_task(
                    self._check_instances_periodically(), name="forgemesh-controller-instances"
                )
            )
        self._workers = [
            asyncio.create_task(self._work(), name=f"forgemesh-controller-worker-{index}")
            for index in range(self._worker_count)
        ]
        logger.info(
            "controller manager started: kinds=%s workers=%d",
            sorted(self._kinds),
            len(self._workers),
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """停止接收新键；worker 处理完手头的资源后退出，超过 ``timeout`` 秒则取消。"""
        self._queue.shutdown()
        for task in self._watchers:
            task.cancel()
        if self._workers:
            _, pending = await asyncio.wait(self._workers, timeout=timeout)
            for task in pending:
                logger.warning("controller worker did not finish in %.1fs; cancelling", timeout)
                task.cancel()
        await asyncio.gather(*self._watchers, *self._workers, return_exceptions=True)
        self._watchers = []
        self._workers = []

    async def resync(self) -> None:
        """全量列出受管资源并加入队列。"""
        for key in await self._registry.list_resource_keys(self._kinds):
            self._queue.add(key)

    async def check_instances(self) -> None:
        """核对一次已部署的实例，把报告的受管资源加入队列；没有注入 ``instance_check`` 时
        什么都不做。"""
        if self._instance_check is None:
            return
        for key in await self._instance_check():
            if key.kind in self._kinds:
                self._queue.add(key)

    async def reclaim(self) -> None:
        """回收一次孤立的分配；没有注入 ``reclaim`` 时什么都不做。"""
        if self._reclaim is not None:
            await self._reclaim()

    async def _reclaim_logging_failures(self) -> None:
        try:
            await self.reclaim()
        except Exception:  # noqa: BLE001 - 下一次全量同步之后重试
            logger.exception("controller allocation reclaim failed")

    async def poll_changes(self) -> int:
        """读取一批新的变更记录，把相关资源加入队列并清理已读记录，返回读到的条数。"""
        changes = await self._registry.changes_since(self._cursor, limit=self._batch_size)
        if not changes:
            return 0
        for change in changes:
            if change.change is not ChangeType.STATUS and change.key.kind in self._kinds:
                self._queue.add(change.key)
        self._cursor = changes[-1].seq
        await self._registry.prune_changes(self._cursor)
        return len(changes)

    async def process(self, key: ResourceKey) -> None:
        """协调一个资源：读取、协调、回写 status、登记去重键、安排下一次。"""
        try:
            resource, phase = await self._registry.get_with_phase(
                key.kind, key.namespace, key.name, key.tenant_id
            )
        except NotFoundError:
            self._queue.forget(key)
            return
        operation = _operation(phase)
        started = time.perf_counter()
        outcome = ReconcileOutcome.ERROR
        try:
            outcome = await self._reconcile(key, resource, phase, operation)
        finally:
            self._metrics.reconciled(operation, outcome, time.perf_counter() - started)

    async def _reconcile(
        self, key: ResourceKey, resource: Resource, phase: str | None, operation: str
    ) -> ReconcileOutcome:
        """协调、回写 status、登记去重键并安排下一次，返回这次协调的结果。"""
        meta = resource.metadata
        dedup_key = compute_dedup_key(
            key.kind, key.namespace, key.name, meta.uid, meta.generation, operation
        ).key
        correlation_id = uuid4().hex
        result = await self._reconciler.reconcile(
            ReconcileContext(
                resource=resource,
                observed={"phase": phase} if phase is not None else {},
                correlation_id=correlation_id,
                tenant_id=key.tenant_id,
                dedup_key=dedup_key,
            )
        )
        if result.status_patch:
            if not await self._write_status(
                key, resource, operation, result.status_patch, correlation_id
            ):
                self._queue.add(key)
                return ReconcileOutcome.CONFLICT
            self._metrics.status_written(phase, result.status_patch)
        if result.done:
            await self._reconciler.mark_processed(dedup_key)
        self._queue.forget(key)
        if result.requeue_after is not None:
            self._queue.add_after(key, result.requeue_after)
        return ReconcileOutcome.of(result)

    def queue_depth(self) -> dict[str, int]:
        """工作队列中各状态的键数（见 :meth:`WorkQueue.depth`）。"""
        return self._queue.depth()

    async def _write_status(
        self,
        key: ResourceKey,
        resource: Resource,
        operation: str,
        patch: dict[str, Any],
        correlation_id: str,
    ) -> bool:
        """以协调前读到的版本 CAS 写入 status；资源已变得不适用这次结果时返回 ``False``。

        ``correlation_id`` 是这次协调的关联 ID，随 status 写入的资源变更事件送出。
        """
        target = (resource.metadata.uid, resource.metadata.generation, operation)
        expected_version = resource.metadata.resourceVersion
        for _ in range(self._status_write_attempts):
            try:
                await self._registry.update_status(
                    key.kind,
                    key.namespace,
                    key.name,
                    key.tenant_id,
                    patch,
                    observed_generation=resource.metadata.generation,
                    expected_version=expected_version,
                    correlation_id=correlation_id,
                )
                return True
            except NotFoundError:
                return False
            except ConflictError:
                try:
                    current, phase = await self._registry.get_with_phase(
                        key.kind, key.namespace, key.name, key.tenant_id
                    )
                except NotFoundError:
                    return False
                if (current.metadata.uid, current.metadata.generation, _operation(phase)) != target:
                    return False
                expected_version = current.metadata.resourceVersion
        return False

    async def _work(self) -> None:
        while (key := await self._queue.get()) is not None:
            try:
                await self.process(key)
            except Exception:  # noqa: BLE001 - 单个资源失败不得影响其他资源
                self._metrics.retried()
                delay = self._queue.add_rate_limited(key)
                logger.exception(
                    "reconcile failed for %s; retrying in %.1fs", _describe(key), delay
                )
            finally:
                self._queue.done(key)

    async def _watch_changes(self) -> None:
        failures = 0
        while True:
            try:
                count = await self.poll_changes()
            except Exception:  # noqa: BLE001 - 存储故障时退避重试，不终止控制器
                failures += 1
                logger.exception("controller failed to read resource changes")
                await asyncio.sleep(
                    min(self._poll_interval * 2**failures, MAX_POLL_BACKOFF_SECONDS)
                )
                continue
            failures = 0
            if count < self._batch_size:
                await asyncio.sleep(self._poll_interval)

    async def _resync_periodically(self) -> None:
        while True:
            await asyncio.sleep(self._resync_period)
            try:
                await self.resync()
            except Exception:  # noqa: BLE001 - 下一个周期重试
                logger.exception("controller resync failed")
            await self._reclaim_logging_failures()

    async def _check_instances_periodically(self) -> None:
        while True:
            await asyncio.sleep(self._instance_check_interval)
            try:
                await self.check_instances()
            except Exception:  # noqa: BLE001 - 下一个周期重试
                self._metrics.instance_check_finished(InstanceCheckOutcome.FAILED)
                logger.exception("controller instance check failed")
            else:
                self._metrics.instance_check_finished(InstanceCheckOutcome.COMPLETED)


__all__ = ["DEFAULT_KINDS", "ControllerManager"]
