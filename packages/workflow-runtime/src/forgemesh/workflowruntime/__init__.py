"""ForgeMesh 工作流运行时参考 Provider 骨架（阶段 0，占位）。

本模块仅定义 **可替换 Provider** 的接口形状与占位实现，不执行任何真实
工作流。真实运行时须实现 ``packages/workflow-api`` 中冻结的
:class:`forgemesh.workflow.runtime.WorkflowRuntime` 契约（9 步闭环：
plan → authorize → prepare → deploy → verify → commit …），并保证：

* 步骤状态机非法转移被拒绝；
* ``commit`` 步骤重新校验资源版本/分配所有权/验收条件，不盲目置 Ready；
* 高风险步骤支持 ``requiresApproval`` 与补偿（compensate）。

阶段 0 的占位实现对所有执行方法抛出 :class:`NotImplementedError`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class WorkflowProvider(ABC):
    """工作流运行时 Provider 抽象基类（占位）。

    与 ``forgemesh.workflow.runtime.WorkflowRuntime`` 对齐的骨架接口；
    真实实现由插件（如 ``workflow-reference``）提供。
    """

    #: Provider 唯一标识。
    name: str = "workflow-reference"

    @abstractmethod
    async def start_run(
        self,
        *,
        definition: str,
        version: int,
        inputs: dict[str, Any],
        tenant_id: str,
        correlation_id: str | None = None,
    ) -> str:
        """启动一次工作流运行，返回 ``run_id``。"""
        raise NotImplementedError

    @abstractmethod
    async def get_run(self, *, run_id: str, tenant_id: str) -> dict[str, Any]:
        """查询运行状态。"""
        raise NotImplementedError

    @abstractmethod
    async def approve(
        self, *, run_id: str, step_id: str, tenant_id: str, approver: str
    ) -> None:
        """对等待人工审批的高风险步骤进行审批。"""
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, *, run_id: str, tenant_id: str, reason: str = "") -> None:
        """取消运行，并触发已执行步骤的补偿。"""
        raise NotImplementedError

    def capabilities(self) -> list[str]:
        """声明 Provider 能力（用于决策路由与兼容性协商）。"""
        return ["workflow.execute", "workflow.approval", "workflow.compensate"]


class ReferenceWorkflowProvider(WorkflowProvider):
    """阶段 0 占位实现：仅记录调用，不执行真实流程。"""

    name = "workflow-reference"

    def __init__(self) -> None:
        #: 已受理但未执行的运行（仅内存，进程退出即丢失）。
        self._accepted: dict[str, dict[str, Any]] = {}

    async def start_run(
        self,
        *,
        definition: str,
        version: int,
        inputs: dict[str, Any],
        tenant_id: str,
        correlation_id: str | None = None,
    ) -> str:
        run_id = f"run-placeholder-{definition}-v{version}"
        self._accepted[run_id] = {
            "definition": definition,
            "version": version,
            "inputs": inputs,
            "tenantId": tenant_id,
            "correlationId": correlation_id,
            "state": "Pending",
        }
        return run_id

    async def get_run(self, *, run_id: str, tenant_id: str) -> dict[str, Any]:
        record = self._accepted.get(run_id)
        if record is None:
            return {"runId": run_id, "state": "Unknown", "tenantId": tenant_id}
        return {"runId": run_id, **record}

    async def approve(
        self, *, run_id: str, step_id: str, tenant_id: str, approver: str
    ) -> None:
        raise NotImplementedError("阶段 0 占位：审批流程尚未实现。")

    async def cancel(self, *, run_id: str, tenant_id: str, reason: str = "") -> None:
        record = self._accepted.get(run_id)
        if record is not None:
            record["state"] = "Cancelled"


__all__ = [
    "WorkflowProvider",
    "ReferenceWorkflowProvider",
]
