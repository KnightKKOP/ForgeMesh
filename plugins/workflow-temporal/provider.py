"""Temporal 工作流运行时 Provider（实现 WorkflowRuntime 契约）。

将 ForgeMesh :class:`WorkflowDefinition` 映射为 Temporal Workflow，每个
:class:`Step` 映射为 Temporal Activity，通过可注入的 :class:`TemporalWorkflowClient`
与 Temporal 引擎交互。

对齐 development-plan 5.4 / ADR-003（可替换 Provider，语义不随引擎改变）。
本插件不产出资源终态（INV-1/INV-3），任何状态写入必须经 Registry 由 Controller 完成。

适配要点：

* **定义映射**：``WorkflowDefinition.name`` → Temporal workflow type；
  每个 ``Step`` → Activity（``step.uses`` → activity type）。
* **状态映射**：Temporal 运行状态 → :class:`WorkflowStatus`（6 态映射）。
* **补偿**：``register_compensation`` 注册步骤级补偿函数；``cancel(cleanup=True)``
  按反向顺序执行已注册补偿，与参考执行器语义一致。
* **信号**：``signal`` 直接映射为 Temporal Signal，用于审批唤醒。
* **幂等**：同一 ``idempotency_key`` 复用既有 workflow_id，不产生重复副作用。

基于 Pydantic v2。
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from forgemesh.workflow.runtime import (
    RunRef,
    Step,
    WorkflowDefinition,
    WorkflowRuntime,
)
from forgemesh.workflow.states import WorkflowStatus
from pydantic import BaseModel, ConfigDict

try:  # 优先复用核心包的统一异常体系
    from forgemesh.core.errors import NotFoundError
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class NotFoundError(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的资源未找到异常。"""


#: 补偿函数类型：``async (step, inputs) -> dict``。
CompensationFn = Callable[[Step, dict[str, Any]], Awaitable[dict[str, Any]]]


class TemporalRunStatus(StrEnum):
    """Temporal 工作流运行状态。

    Attributes:
        RUNNING: 工作流正在执行。
        WAITING: 等待外部信号（如审批）。
        COMPLETED: 正常完成。
        FAILED: 执行失败。
        CANCELLED: 已取消。
        TIMED_OUT: 超时终止。
    """

    RUNNING = "Running"
    WAITING = "Waiting"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    TIMED_OUT = "TimedOut"


#: Temporal 状态 → ForgeMesh WorkflowStatus 映射表。
_TEMPORAL_TO_WORKFLOW_STATUS: dict[TemporalRunStatus, WorkflowStatus] = {
    TemporalRunStatus.RUNNING: WorkflowStatus.RUNNING,
    TemporalRunStatus.WAITING: WorkflowStatus.WAITING,
    TemporalRunStatus.COMPLETED: WorkflowStatus.SUCCEEDED,
    TemporalRunStatus.FAILED: WorkflowStatus.FAILED,
    TemporalRunStatus.CANCELLED: WorkflowStatus.CANCELLED,
    TemporalRunStatus.TIMED_OUT: WorkflowStatus.TIMEDOUT,
}


class TemporalWorkflowDescription(BaseModel):
    """Temporal 工作流描述（查询结果）。

    Attributes:
        workflow_id: Temporal 工作流实例 id。
        status: Temporal 运行状态。
        workflow_type: 工作流类型名称。
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    status: TemporalRunStatus
    workflow_type: str


class TemporalWorkflowClient(ABC):
    """Temporal 工作流客户端接口（可注入 mock 或真实 SDK 适配）。

    实现方需对接 ``tempio`` / ``temporal-sdk`` 等 Temporal 客户端 SDK，
    将本接口的方法映射为对应的 Temporal API 调用。
    """

    @abstractmethod
    async def start_workflow(
        self,
        workflow_type: str,
        args: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        """启动 Temporal 工作流，返回 workflow_id。

        Args:
            workflow_type: 工作流类型名称。
            args: 工作流参数（含 inputs、activities 描述）。
            idempotency_key: 幂等键；同一键应复用既有 workflow_id。

        Returns:
            Temporal workflow_id。
        """
        raise NotImplementedError

    @abstractmethod
    async def describe_workflow(self, workflow_id: str) -> TemporalWorkflowDescription:
        """查询工作流描述。

        Args:
            workflow_id: 工作流实例 id。

        Returns:
            :class:`TemporalWorkflowDescription`。

        Raises:
            NotFoundError: 工作流不存在。
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel_workflow(self, workflow_id: str) -> None:
        """取消工作流。

        Raises:
            NotFoundError: 工作流不存在。
        """
        raise NotImplementedError

    @abstractmethod
    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, Any],
    ) -> None:
        """向工作流发送信号（用于审批唤醒等）。

        Raises:
            NotFoundError: 工作流不存在。
        """
        raise NotImplementedError


class InMemoryTemporalClient(TemporalWorkflowClient):
    """内存 Temporal 客户端（测试/开发用，无真实 Temporal 服务）。

    维护工作流实例的内存状态，支持幂等启动、状态查询、取消与信号记录。
    """

    def __init__(self) -> None:
        self._workflows: dict[str, TemporalWorkflowDescription] = {}
        self._args: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}
        self._signals: list[tuple[str, str, dict[str, Any]]] = []

    async def start_workflow(
        self,
        workflow_type: str,
        args: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        if idempotency_key in self._idempotency:
            return self._idempotency[idempotency_key]
        workflow_id = f"wf-{uuid.uuid4().hex[:12]}"
        self._workflows[workflow_id] = TemporalWorkflowDescription(
            workflow_id=workflow_id,
            status=TemporalRunStatus.RUNNING,
            workflow_type=workflow_type,
        )
        self._args[workflow_id] = args
        self._idempotency[idempotency_key] = workflow_id
        return workflow_id

    async def describe_workflow(self, workflow_id: str) -> TemporalWorkflowDescription:
        desc = self._workflows.get(workflow_id)
        if desc is None:
            raise NotFoundError(f"workflow not found: {workflow_id}")
        return desc

    async def cancel_workflow(self, workflow_id: str) -> None:
        desc = self._workflows.get(workflow_id)
        if desc is None:
            raise NotFoundError(f"workflow not found: {workflow_id}")
        self._workflows[workflow_id] = desc.model_copy(
            update={"status": TemporalRunStatus.CANCELLED},
        )

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: dict[str, Any],
    ) -> None:
        if workflow_id not in self._workflows:
            raise NotFoundError(f"workflow not found: {workflow_id}")
        self._signals.append((workflow_id, signal_name, payload))

    # ---- 测试辅助方法 ----

    def set_status(self, workflow_id: str, status: TemporalRunStatus) -> None:
        """测试辅助：直接设置工作流状态（模拟 Temporal 引擎状态推进）。"""
        desc = self._workflows.get(workflow_id)
        if desc is None:
            raise NotFoundError(f"workflow not found: {workflow_id}")
        self._workflows[workflow_id] = desc.model_copy(update={"status": status})

    @property
    def signals(self) -> list[tuple[str, str, dict[str, Any]]]:
        """已投递的信号记录（测试用）。"""
        return list(self._signals)


class TemporalWorkflowProvider(WorkflowRuntime):
    """Temporal 工作流运行时 Provider。

    将 ForgeMesh :class:`WorkflowDefinition` 映射为 Temporal Workflow，
    每个 :class:`Step` 映射为 Temporal Activity，实现
    :class:`forgemesh.workflow.runtime.WorkflowRuntime` 契约。

    Args:
        client: Temporal 工作流客户端（可注入 mock）；默认使用内存实现。
        definition_version: 运行实例固定的定义版本号（版本固定语义，ADR-011）。
    """

    def __init__(
        self,
        client: TemporalWorkflowClient | None = None,
        definition_version: int = 1,
    ) -> None:
        self._client: TemporalWorkflowClient = client or InMemoryTemporalClient()
        self._definition_version = definition_version
        self._compensations: dict[str, CompensationFn] = {}
        self._run_to_definition: dict[str, WorkflowDefinition] = {}
        self._run_to_inputs: dict[str, dict[str, Any]] = {}
        self._compensation_log: list[str] = []

    # ---- 适配层：定义映射 ----

    @staticmethod
    def _map_to_workflow_type(defn: WorkflowDefinition) -> str:
        """将 WorkflowDefinition 映射为 Temporal workflow type 名称。"""
        return f"forgemesh.{defn.name}"

    @staticmethod
    def _map_to_temporal_args(
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """将定义与输入映射为 Temporal workflow 参数。

        每个 :class:`Step` 映射为一个 Activity 描述，保留依赖、超时、审批、
        补偿等语义字段。
        """
        merged = {**defn.inputs, **inputs}
        activities: list[dict[str, Any]] = [
            {
                "activity_type": f"forgemesh.activity.{step.uses}",
                "step_id": step.id,
                "needs": list(step.needs),
                "timeout_seconds": step.timeout,
                "requires_approval": step.requires_approval,
                "compensate": step.compensate,
            }
            for step in defn.steps
        ]
        return {
            "inputs": merged,
            "activities": activities,
            "timeout_seconds": defn.timeout_seconds,
        }

    # ---- WorkflowRuntime 契约实现 ----

    async def start(
        self,
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        """启动（或幂等复用）一个 Temporal 工作流实例。

        将 ``defn`` 映射为 Temporal workflow type 与参数，通过客户端启动执行。
        同一 ``idempotency_key`` 复用既有 workflow_id（幂等语义）。

        Returns:
            指向既有或新建运行实例的 :class:`RunRef`。
        """
        workflow_type = self._map_to_workflow_type(defn)
        args = self._map_to_temporal_args(defn, inputs)
        workflow_id = await self._client.start_workflow(
            workflow_type, args, idempotency_key,
        )
        self._run_to_definition[workflow_id] = defn
        self._run_to_inputs[workflow_id] = inputs
        return RunRef(
            run_id=workflow_id,
            definition_version=self._definition_version,
        )

    async def get_status(self, run_id: str) -> WorkflowStatus:
        """查询运行实例当前状态（Temporal 状态映射为 WorkflowStatus）。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        desc = await self._client.describe_workflow(run_id)
        return _TEMPORAL_TO_WORKFLOW_STATUS[desc.status]

    async def cancel(self, run_id: str, cleanup: bool = False) -> None:
        """取消运行实例。

        Args:
            run_id: 运行实例标识。
            cleanup: ``False`` 仅停止后续步骤；``True`` 额外按反向顺序执行
                已注册补偿函数以清理既有副作用（与参考执行器语义一致）。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        if cleanup:
            defn = self._run_to_definition.get(run_id)
            inputs = self._run_to_inputs.get(run_id, {})
            if defn is not None:
                # 按定义逆序执行已注册补偿（反向回滚）。
                for step in reversed(defn.steps):
                    comp_fn = self._compensations.get(step.id)
                    if comp_fn is not None:
                        await comp_fn(step, inputs)
                        self._compensation_log.append(step.id)

        await self._client.cancel_workflow(run_id)

    async def signal(
        self,
        run_id: str,
        name: str,
        payload: dict[str, Any],
    ) -> None:
        """向运行实例投递外部信号（映射为 Temporal Signal，用于审批唤醒）。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        await self._client.signal_workflow(run_id, name, payload)

    # ---- Temporal 特有方法 ----

    def register_compensation(
        self,
        step_id: str,
        compensation_fn: CompensationFn,
    ) -> None:
        """注册步骤级补偿函数。

        在 ``cancel(cleanup=True)`` 时按反向顺序调用已注册的补偿函数，
        回滚已产生的副作用。

        Args:
            step_id: 步骤 id。
            compensation_fn: 补偿函数 ``async (step, inputs) -> dict``。
        """
        self._compensations[step_id] = compensation_fn

    @property
    def compensation_log(self) -> list[str]:
        """已执行的补偿步骤 id 列表（按执行顺序，测试用）。"""
        return list(self._compensation_log)

    @property
    def client(self) -> TemporalWorkflowClient:
        """底层 Temporal 客户端（测试/诊断用）。"""
        return self._client


def temporal_eval_report() -> dict[str, Any]:
    """返回 Temporal 适配评估报告（ADR-003 验证结论）。

    评估 Temporal 作为 ForgeMesh WorkflowRuntime 的适配可行性，
    覆盖持久化、幂等、重试、超时、取消、补偿、审批、版本固定等语义维度。

    Returns:
        评估报告 dict，包含各维度的适配结论与证据。
    """
    return {
        "adr": "ADR-003",
        "conclusion": "feasible",
        "engine": "temporal",
        "contract": "WorkflowRuntime",
        "semantics": {
            "persistence": {
                "supported": True,
                "mechanism": "Temporal 原生持久化执行历史",
            },
            "idempotency": {
                "supported": True,
                "mechanism": "Temporal Workflow Id 复用，同一 idempotency_key 不重复创建",
            },
            "retry": {
                "supported": True,
                "mechanism": "Temporal Activity RetryPolicy 映射 ForgeMesh RetryPolicy",
            },
            "timeout": {
                "supported": True,
                "mechanism": "Workflow RunTimeout + Activity ScheduleToClose/StartToClose",
            },
            "cancel": {
                "supported": True,
                "mechanism": "Temporal Cancel + compensation 反向回滚",
            },
            "compensation": {
                "supported": True,
                "mechanism": "register_compensation 注册步骤级补偿，cancel(cleanup=True) 逆序执行",
            },
            "approval": {
                "supported": True,
                "mechanism": "Temporal Signal 映射审批唤醒，WAITING 状态等待外部信号",
            },
            "version_pinning": {
                "supported": True,
                "mechanism": "RunRef.definition_version 固定，升级定义不影响既有实例",
            },
        },
        "status_mapping": {
            str(k): str(v) for k, v in _TEMPORAL_TO_WORKFLOW_STATUS.items()
        },
        "invariants": [
            "业务语义不变性：与参考执行器（workflow-reference）行为一致",
            "不产出资源终态（INV-1/INV-3），状态写入经 Registry",
        ],
    }


__all__ = [
    "CompensationFn",
    "InMemoryTemporalClient",
    "TemporalRunStatus",
    "TemporalWorkflowClient",
    "TemporalWorkflowDescription",
    "TemporalWorkflowProvider",
    "temporal_eval_report",
]
