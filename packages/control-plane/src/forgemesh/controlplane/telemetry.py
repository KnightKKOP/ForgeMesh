"""控制平面暴露的指标（``GET /metrics``，Prometheus 文本格式 0.0.4）。

全部指标在这里定义，标签只取有限的值，不含租户、名称、命名空间、uid、地址等租户数据：

* ``forgemesh_http_requests_total{method,route,status}``：HTTP 请求数。``route`` 是路由
  模板（如 ``/api/v1/inference-services/{name}``），没有匹配任何路由的请求记为
  ``unmatched``；``method`` 不是常见方法时记为 ``OTHER``。
* ``forgemesh_http_request_duration_seconds{method,route}``：处理 HTTP 请求的耗时。
* ``forgemesh_reconcile_total{operation,outcome}``：控制器的协调次数。``operation`` 为
  ``reconcile`` / ``recover``（``Degraded``）/ ``reclaim``（``Failed``）/ ``terminate``
  （``Terminating``）；``outcome`` 见 :class:`ReconcileOutcome`。
* ``forgemesh_reconcile_duration_seconds{operation}``：一次协调的耗时，含 status 回写。
* ``forgemesh_controller_retries_total``：协调抛出异常、按退避重新排队的次数。
* ``forgemesh_controller_queue_depth{state}``：工作队列中的键数，``state`` 为 ``ready``
  （可立即处理）、``delayed``（等待延迟到期）、``processing``（处理中）。
* ``forgemesh_deployments_total{outcome,reason}``：推理服务从别的阶段进入 ``Ready``
  （``outcome="ready"``）或 ``Failed``（``outcome="failed"``）的次数，在 status 写入成功后
  计数；``reason`` 为 ``Ready`` 条件的原因（如 ``Reconciled`` / ``WorkflowFailed``），不是
  标识符形式时记为 ``Unknown``。
* ``forgemesh_inference_services{phase}``：各阶段的推理服务数，每个阶段都输出。
* ``forgemesh_allocations_active{accelerator_type}``：Active 分配数，按其设备在加速器清单中的
  类型；设备不在清单中（或类型不一）时记为 ``unknown``，没有设备的分配记为 ``none``。
* ``forgemesh_accelerator_devices{accelerator_type,state}``：清单中的设备数，``state`` 为
  ``free`` / ``allocated``；未配置清单时没有样本。
* ``forgemesh_workflow_runs{status}``：各状态的部署工作流运行数，每个状态都输出；未配置启动器
  时没有样本。
* ``forgemesh_database_lock_wait_seconds``：一次数据库操作等待控制平面数据库连接锁的时间。
  资源注册表、Outbox（含事件发布）与审计记录共用这个连接与锁（见
  :class:`~forgemesh.controlplane.sqlite.Database`）；不含 SQLite 内部的忙等待。

仪表盘（队列深度、服务、分配、设备与工作流运行数）在抓取时从控制器、注册中心、加速器清单与
工作流存储读取（见 :meth:`ControlPlaneMetrics.watch`），跨租户聚合；锁等待时间在每次数据库操作
时记录。加速器类型取自运维配置的清单，不是租户数据。

``/metrics`` 与探针一样不要求认证，只输出聚合数据；它不在 ``/api/v1`` 之下，网关不会
转发它（见 README「可观测性」）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from forgemesh.resource.inference import InferencePhase
from forgemesh.workflow.states import WorkflowStatus

from .metrics import MetricsRegistry

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用
    from forgemesh.core.reconciler import ReconcileResult
    from forgemesh.workflowruntime.store import WorkflowStore

    from .controller import ControllerManager
    from .registry import SQLiteRegistry
    from .scheduler import Inventory

#: 记作 ``method`` 标签的 HTTP 方法，其余方法记为 :data:`OTHER_METHOD`。
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
OTHER_METHOD = "OTHER"
#: 没有匹配任何路由的请求的 ``route`` 标签。
UNMATCHED_ROUTE = "unmatched"

#: ``forgemesh_controller_queue_depth`` 的 ``state`` 取值。
QUEUE_STATES = ("ready", "delayed", "processing")
#: 设备不在加速器清单中（或一个分配的设备类型不一）时的 ``accelerator_type``。
UNKNOWN_ACCELERATOR_TYPE = "unknown"
#: 没有设备的分配的 ``accelerator_type``。
NO_ACCELERATOR_TYPE = "none"
#: 数据库锁等待的直方图桶上界（秒）。
LOCK_WAIT_BUCKETS = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5)

_INFERENCE_SERVICE = "InferenceService"
_DEPLOYMENT_OUTCOMES = {InferencePhase.READY.value: "ready", InferencePhase.FAILED.value: "failed"}
_REASON = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")
_UNKNOWN_REASON = "Unknown"


class ReconcileOutcome(StrEnum):
    """一次协调的结果（``forgemesh_reconcile_total`` 的 ``outcome`` 标签）。"""

    #: 当前 generation 已处理完毕。
    DONE = "done"
    #: 按协调器给出的延迟重新排队。
    REQUEUE = "requeue"
    #: 不定时重试，等待资源变更或下一次全量同步。
    WAIT = "wait"
    #: 资源在协调期间已变化（或已删除），结果被丢弃并重新排队。
    CONFLICT = "conflict"
    #: 协调抛出异常（随后按退避重试）。
    ERROR = "error"

    @classmethod
    def of(cls, result: ReconcileResult) -> ReconcileOutcome:
        """status 写入成功（或无需写入）之后，按协调结果归类。"""
        if result.done:
            return cls.DONE
        return cls.REQUEUE if result.requeue_after is not None else cls.WAIT


def _ready_reason(patch: Mapping[str, Any]) -> str:
    for condition in patch.get("conditions") or ():
        if isinstance(condition, Mapping) and condition.get("type") == "Ready":
            reason = condition.get("reason")
            if isinstance(reason, str) and _REASON.fullmatch(reason):
                return reason
    return _UNKNOWN_REASON


class HTTPMetrics:
    """HTTP 请求的计数与耗时。"""

    def __init__(self, registry: MetricsRegistry) -> None:
        self._requests = registry.counter(
            "forgemesh_http_requests_total",
            "HTTP requests handled by the control plane, by method, route template and status.",
            ("method", "route", "status"),
        )
        self._duration = registry.histogram(
            "forgemesh_http_request_duration_seconds",
            "Time spent handling HTTP requests, by method and route template.",
            ("method", "route"),
        )

    def observe(self, method: str, route: str | None, status: int, seconds: float) -> None:
        """记录一个请求；``route`` 为路由模板，没有匹配路由时为 ``None``。"""
        method = method if method in HTTP_METHODS else OTHER_METHOD
        route = route or UNMATCHED_ROUTE
        self._requests.inc(method, route, str(status))
        self._duration.observe(seconds, method, route)


class ControllerMetrics:
    """控制器的协调次数与耗时、重试、部署结果与工作队列深度。

    Args:
        registry: 指标所在的注册中心；为空时新建（指标不会被导出）。
    """

    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        registry = registry if registry is not None else MetricsRegistry()
        self._reconciles = registry.counter(
            "forgemesh_reconcile_total",
            "Reconciliations by operation and outcome.",
            ("operation", "outcome"),
        )
        self._duration = registry.histogram(
            "forgemesh_reconcile_duration_seconds",
            "Time spent reconciling one resource, including the status write, by operation.",
            ("operation",),
        )
        self._retries = registry.counter(
            "forgemesh_controller_retries_total",
            "Reconciliations that raised and were requeued with backoff.",
        )
        self._deployments = registry.counter(
            "forgemesh_deployments_total",
            "Inference services that entered Ready or Failed, by outcome and Ready reason.",
            ("outcome", "reason"),
        )
        #: 由 :meth:`ControlPlaneMetrics.watch` 登记的采集器在抓取时更新。
        self.queue_depth = registry.gauge(
            "forgemesh_controller_queue_depth",
            "Keys in the controller work queue, by state.",
            ("state",),
        )

    def reconciled(self, operation: str, outcome: ReconcileOutcome, seconds: float) -> None:
        """记录一次协调的结果与耗时。"""
        self._reconciles.inc(operation, outcome.value)
        self._duration.observe(seconds, operation)

    def retried(self) -> None:
        """记录一次抛出异常之后的退避重试。"""
        self._retries.inc()

    def status_written(self, phase: str | None, patch: Mapping[str, Any]) -> None:
        """记录一次成功的 status 写入；``phase`` 是写入之前的阶段。"""
        target = patch.get("phase")
        outcome = _DEPLOYMENT_OUTCOMES.get(target) if isinstance(target, str) else None
        if outcome is not None and target != phase:
            self._deployments.inc(outcome, _ready_reason(patch))


class ControlPlaneMetrics:
    """控制平面的指标注册中心及各组件的指标。"""

    def __init__(self) -> None:
        self.registry = MetricsRegistry()
        self.http = HTTPMetrics(self.registry)
        self.controller = ControllerMetrics(self.registry)
        self._services = self.registry.gauge(
            "forgemesh_inference_services",
            "Inference services by phase.",
            ("phase",),
        )
        self._allocations = self.registry.gauge(
            "forgemesh_allocations_active",
            "Active accelerator allocations, by accelerator type of their devices.",
            ("accelerator_type",),
        )
        self._devices = self.registry.gauge(
            "forgemesh_accelerator_devices",
            "Accelerator devices in the inventory, by type and state (free or allocated).",
            ("accelerator_type", "state"),
        )
        self._workflow_runs = self.registry.gauge(
            "forgemesh_workflow_runs",
            "Deployment workflow runs by status.",
            ("status",),
        )
        self._lock_wait = self.registry.histogram(
            "forgemesh_database_lock_wait_seconds",
            "Time database operations of the registry, the outbox and the audit log waited "
            "for their shared connection lock.",
            buckets=LOCK_WAIT_BUCKETS,
        )

    def watch(
        self,
        registry: SQLiteRegistry,
        controller: ControllerManager,
        inventory: Inventory | None = None,
        workflow_store: WorkflowStore | None = None,
    ) -> Callable[[], None]:
        """在抓取时从这些组件读取状态类指标，并观测注册中心所在数据库的锁等待。

        返回注销函数：之后不再读取这些组件，状态类指标没有样本。
        """
        device_types = {
            (node.id, device.id): device.type
            for node in (inventory.nodes if inventory is not None else ())
            for device in node.devices
        }

        async def queue() -> None:
            depth = controller.queue_depth()
            self.controller.queue_depth.replace({(state,): depth[state] for state in QUEUE_STATES})

        async def services() -> None:
            counts = dict.fromkeys((phase.value for phase in InferencePhase), 0)
            for phase, count in (await registry.count_by_phase(_INFERENCE_SERVICE)).items():
                counts[phase if phase in counts else InferencePhase.UNKNOWN.value] += count
            self._services.replace({(phase,): count for phase, count in counts.items()})

        async def allocations() -> None:
            active = await registry.active_allocation_devices()
            by_type = {(accelerator,): 0 for accelerator in device_types.values()}
            for devices in active.values():
                types = {
                    device_types.get((d.node_id, d.device_id), UNKNOWN_ACCELERATOR_TYPE)
                    for d in devices
                }
                if not types:
                    accelerator = NO_ACCELERATOR_TYPE
                else:
                    accelerator = types.pop() if len(types) == 1 else UNKNOWN_ACCELERATOR_TYPE
                by_type[(accelerator,)] = by_type.get((accelerator,), 0) + 1
            taken = {(d.node_id, d.device_id) for devices in active.values() for d in devices}
            by_state = {
                (accelerator, state): 0
                for accelerator in device_types.values()
                for state in ("free", "allocated")
            }
            for device, accelerator in device_types.items():
                by_state[(accelerator, "allocated" if device in taken else "free")] += 1
            self._allocations.replace(by_type)
            self._devices.replace(by_state)

        removers = [
            self.registry.add_collector(queue, [self.controller.queue_depth]),
            self.registry.add_collector(services, [self._services]),
            self.registry.add_collector(allocations, [self._allocations, self._devices]),
        ]
        if workflow_store is not None:
            store = workflow_store

            async def workflow_runs() -> None:
                counts = await store.count_runs_by_status()
                self._workflow_runs.replace(
                    {(status.value,): counts.get(status.value, 0) for status in WorkflowStatus}
                )

            removers.append(self.registry.add_collector(workflow_runs, [self._workflow_runs]))
        registry.database.set_lock_wait_observer(self._lock_wait.observe)

        def unwatch() -> None:
            registry.database.set_lock_wait_observer(None)
            for remove in removers:
                remove()
            for gauge in (
                self.controller.queue_depth,
                self._services,
                self._allocations,
                self._devices,
                self._workflow_runs,
            ):
                gauge.replace({})

        return unwatch


__all__ = [
    "HTTP_METHODS",
    "LOCK_WAIT_BUCKETS",
    "NO_ACCELERATOR_TYPE",
    "OTHER_METHOD",
    "QUEUE_STATES",
    "UNKNOWN_ACCELERATOR_TYPE",
    "UNMATCHED_ROUTE",
    "ControlPlaneMetrics",
    "ControllerMetrics",
    "HTTPMetrics",
    "ReconcileOutcome",
]
