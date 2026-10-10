"""参考工作流运行时插件（实现 WorkflowRuntime 契约）。

组合已有的 WorkflowStore + WorkflowExecutor + CompensationExecutor + ApprovalGate，
提供完整的「持久化 + 幂等执行 + 补偿 + 审批」参考实现。

对齐 development-plan 5.4 / ADR-003。本插件不产出资源终态（INV-1/INV-3），
任何状态写入必须经 Registry 由 Controller 完成。

兼容 Python 3.9+。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from forgemesh.workflow.runtime import (
    RunRef,
    WorkflowDefinition,
    WorkflowRuntime,
)
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.approval import ApprovalGate
from forgemesh.workflowruntime.compensate import CompensationExecutor
from forgemesh.workflowruntime.exec import WorkflowExecutor
from forgemesh.workflowruntime.store import WorkflowStore

#: 步骤 handler 类型：async (step, inputs) -> dict。
StepHandler = Callable[..., Awaitable[dict[str, Any]]]


class ReferenceWorkflowRuntime(WorkflowRuntime):
    """参考工作流运行时实现。

    组合持久化存储、执行引擎、补偿执行器与审批门控，
    实现 :class:`forgemesh.workflow.runtime.WorkflowRuntime` 契约。

    Args:
        store: 工作流持久化存储。
        executor: 工作流执行器。
        compensation: 补偿执行器。
        approval: 审批门控。
        handler: 步骤执行 handler ``async (step, inputs) -> dict``。
    """

    def __init__(
        self,
        store: WorkflowStore,
        executor: WorkflowExecutor,
        compensation: CompensationExecutor,
        approval: ApprovalGate,
        handler: StepHandler,
    ) -> None:
        self._store = store
        self._executor = executor
        self._compensation = compensation
        self._approval = approval
        self._handler = handler

    async def start(
        self,
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        """启动（或幂等复用）一个运行实例。

        通过 ``store.create_run`` 创建运行实例（幂等：同一 ``idempotency_key``
        返回已有实例）。创建后将状态从 ``Pending`` 推进到 ``Running``。

        Returns:
            指向既有或新建运行实例的 :class:`RunRef`。
        """
        ref = await self._store.create_run(defn, inputs, idempotency_key)

        # 若运行实例仍为 Pending，推进到 Running。
        run = await self._store.get_run(ref.run_id)
        if run["status"] == WorkflowStatus.PENDING.value:
            await self._store.update_run_status(
                ref.run_id, WorkflowStatus.RUNNING.value
            )

        return ref

    async def get_status(self, run_id: str) -> WorkflowStatus:
        """查询运行实例当前状态。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        run = await self._store.get_run(run_id)
        return WorkflowStatus(run["status"])

    async def cancel(self, run_id: str, cleanup: bool = False) -> None:
        """取消运行实例。

        Args:
            run_id: 运行实例标识。
            cleanup: ``False`` 仅更新状态为 ``Cancelled``；
                ``True`` 额外执行补偿以清理已产生副作用。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        run = await self._store.get_run(run_id)

        if cleanup:
            # 收集已成功完成的步骤（按定义顺序），执行补偿。
            completed_steps: list[str] = []
            step_results: dict[str, Any] = {}
            for step_info in run["steps"]:
                if step_info["status"] == StepStatus.SUCCEEDED.value:
                    completed_steps.append(step_info["step_id"])
                    step_results[step_info["step_id"]] = step_info.get("result")

            # 反序列化工作流定义以供补偿执行器使用。
            defn = WorkflowDefinition.model_validate_json(run["definition_json5"])
            await self._compensation.compensate(defn, completed_steps, step_results)

        await self._store.update_run_status(
            run_id, WorkflowStatus.CANCELLED.value
        )

    async def signal(
        self, run_id: str, name: str, payload: dict[str, Any]
    ) -> None:
        """向运行实例投递外部信号，唤醒处于 ``Waiting`` 的步骤/审批。

        支持的信号名称：
        * ``"approve"`` —— 审批通过，唤醒指定步骤。
        * ``"reject"`` —— 审批拒绝，唤醒指定步骤。

        Args:
            run_id: 运行实例标识。
            name: 信号名称。
            payload: 信号载荷，须包含 ``step_id``。

        Raises:
            NotFoundError: 运行实例不存在。
            ValueError: 不支持的信号名称或缺少 ``step_id``。
        """
        # 验证运行实例存在。
        await self._store.get_run(run_id)

        step_id = payload.get("step_id")
        if not step_id:
            raise ValueError("signal payload must contain 'step_id'")

        if name == "approve":
            self._approval.approve(run_id, step_id)
        elif name == "reject":
            self._approval.reject(run_id, step_id)
        else:
            raise ValueError(f"unsupported signal: {name!r}")


__all__ = [
    "ReferenceWorkflowRuntime",
    "StepHandler",
]
