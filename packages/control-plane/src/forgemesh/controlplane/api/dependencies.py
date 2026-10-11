"""API 依赖注入：Registry 实例、运行时组件与租户上下文。

组件组装模式（对齐 development-plan 8.2「组装与依赖注入」）：

* ``Registry`` —— 由 :func:`init_registry` 初始化，未初始化时以内存库兜底；
* ``WorkflowRuntime`` / ``DecisionRouter`` —— 由组装方（部署入口/测试）通过
  :func:`init_workflow_runtime` / :func:`init_decision_router` 显式注入；
  未注入时 :func:`get_workflow_runtime` / :func:`get_decision_router` 抛出
  :class:`ComponentNotReadyError`，对应端点返回 503 错误信封（诚实暴露
  组装缺失，而非 404/501 残留）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Header, HTTPException
from forgemesh.controlplane.registry import SQLiteRegistry

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用，运行时不导入
    from forgemesh.decision.router import DecisionRouter
    from forgemesh.workflow.runtime import WorkflowRuntime

#: 全局 Registry 实例（由 ``init_registry`` 初始化）。
_registry: SQLiteRegistry | None = None

#: 全局 WorkflowRuntime 实例（由 ``init_workflow_runtime`` 注入）。
_workflow_runtime: WorkflowRuntime | None = None

#: 全局 DecisionRouter 实例（由 ``init_decision_router`` 注入）。
_decision_router: DecisionRouter | None = None


class ComponentNotReadyError(RuntimeError):
    """运行时组件未注入（组装方未调用对应的 ``init_*``）。"""


def init_registry(db_path: str = ":memory:") -> SQLiteRegistry:
    """初始化全局 Registry 实例。

    Args:
        db_path: SQLite 数据库路径，默认 ``:memory:``（内存库，单测友好）。

    Returns:
        初始化后的 :class:`SQLiteRegistry` 实例。
    """
    global _registry
    _registry = SQLiteRegistry(db_path)
    return _registry


def get_registry() -> SQLiteRegistry:
    """获取当前 Registry 实例；未初始化时以内存库兜底。"""
    if _registry is None:
        return init_registry()
    return _registry


def init_workflow_runtime(runtime: WorkflowRuntime) -> WorkflowRuntime:
    """注入全局 WorkflowRuntime 实例（``POST /api/v1/workflows`` 使用）。

    Args:
        runtime: 满足 ``forgemesh.workflow.runtime.WorkflowRuntime`` 契约的实例
            （``start(defn, inputs, idempotency_key) -> RunRef``）。

    Returns:
        注入的实例（便于链式组装）。
    """
    global _workflow_runtime
    _workflow_runtime = runtime
    return runtime


def get_workflow_runtime() -> WorkflowRuntime:
    """获取已注入的 WorkflowRuntime；未注入时抛 :class:`ComponentNotReadyError`。"""
    if _workflow_runtime is None:
        raise ComponentNotReadyError(
            "WorkflowRuntime 未注入：请先调用 init_workflow_runtime(...) 完成组装。"
        )
    return _workflow_runtime


def init_decision_router(router: DecisionRouter) -> DecisionRouter:
    """注入全局 DecisionRouter 实例（``POST /api/v1/decisions`` 使用）。

    Args:
        router: :class:`forgemesh.decision.router.DecisionRouter` 实例
            （或提供 ``route(req) -> DecisionResult`` 的可替换实现）。

    Returns:
        注入的实例（便于链式组装）。
    """
    global _decision_router
    _decision_router = router
    return router


def get_decision_router() -> DecisionRouter:
    """获取已注入的 DecisionRouter；未注入时抛 :class:`ComponentNotReadyError`。"""
    if _decision_router is None:
        raise ComponentNotReadyError(
            "DecisionRouter 未注入：请先调用 init_decision_router(...) 完成组装。"
        )
    return _decision_router


def reset_runtime_components() -> None:
    """清空已注入的 WorkflowRuntime / DecisionRouter（测试与重组装用）。"""
    global _workflow_runtime, _decision_router
    _workflow_runtime = None
    _decision_router = None


async def get_tenant_id(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> str:
    """从请求头 ``X-Tenant-Id`` 提取租户 ID（强制要求）。

    缺失时返回 HTTP 400。租户上下文为多租户隔离的权威来源，
    覆盖请求体中可能携带的租户字段。
    """
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-Id header is required")
    return x_tenant_id
