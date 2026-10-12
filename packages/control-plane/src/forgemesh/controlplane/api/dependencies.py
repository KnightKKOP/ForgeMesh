"""API 依赖注入：Registry 实例、运行时组件与已认证的调用主体。

组件组装模式（对齐 development-plan 8.2「组装与依赖注入」）：

* ``Registry`` —— 由 :func:`init_registry` 初始化，未初始化时以内存库兜底；
* ``WorkflowRuntime`` / ``DecisionRouter`` —— 由组装方（部署入口/测试）通过
  :func:`init_workflow_runtime` / :func:`init_decision_router` 显式注入；
  未注入时 :func:`get_workflow_runtime` / :func:`get_decision_router` 抛出
  :class:`ComponentNotReadyError`，对应端点返回 503 错误信封（诚实暴露
  组装缺失，而非 404/501 残留）；
* ``Authenticator`` —— 由 :func:`init_authenticator` 注入。未注入时所有业务
  端点返回 503，绝不退化为信任请求头。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Depends, Request
from forgemesh.controlplane.auth import (
    SCOPE_READ,
    SCOPE_WRITE,
    AuthenticationError,
    Authenticator,
    PermissionDeniedError,
    Principal,
)
from forgemesh.controlplane.registry import SQLiteRegistry

from .errors import ApiError

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用，运行时不导入
    from forgemesh.decision.router import DecisionRouter
    from forgemesh.workflow.runtime import WorkflowRuntime

#: 全局 Registry 实例（由 ``init_registry`` 初始化）。
_registry: SQLiteRegistry | None = None

#: 全局 WorkflowRuntime 实例（由 ``init_workflow_runtime`` 注入）。
_workflow_runtime: WorkflowRuntime | None = None

#: 全局 DecisionRouter 实例（由 ``init_decision_router`` 注入）。
_decision_router: DecisionRouter | None = None

#: 全局认证器（由 ``init_authenticator`` 注入）。
_authenticator: Authenticator | None = None

#: 401 响应附带的质询头（RFC 6750）。
_BEARER_CHALLENGE = 'Bearer realm="forgemesh"'


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


def init_authenticator(authenticator: Authenticator) -> Authenticator:
    """注入全局认证器（见 :mod:`forgemesh.controlplane.auth`）。

    Returns:
        注入的实例（便于链式组装）。
    """
    global _authenticator
    _authenticator = authenticator
    return authenticator


def get_authenticator() -> Authenticator:
    """获取已注入的认证器；未注入时抛 :class:`ComponentNotReadyError`。"""
    if _authenticator is None:
        raise ComponentNotReadyError(
            "Authenticator 未注入：请先调用 init_authenticator(...) 完成组装。"
        )
    return _authenticator


def reset_authenticator() -> None:
    """清空已注入的认证器（测试与重组装用）。"""
    global _authenticator
    _authenticator = None


async def get_principal(request: Request) -> Principal:
    """校验请求凭证，返回已验证的调用主体。

    认证器未注入返回 503（组装缺失时拒绝而不是放行）；凭证缺失或无效返回 401
    （附 ``WWW-Authenticate``）；凭证有效但越权（如租户头与凭证不符）返回 403。
    """
    try:
        authenticator = get_authenticator()
    except ComponentNotReadyError as exc:
        raise ApiError(503, "AUTHENTICATOR_UNAVAILABLE", str(exc)) from exc
    try:
        return authenticator.authenticate(request.headers)
    except AuthenticationError as exc:
        raise ApiError(
            401, "UNAUTHENTICATED", str(exc), headers={"WWW-Authenticate": _BEARER_CHALLENGE}
        ) from exc
    except PermissionDeniedError as exc:
        raise ApiError(403, "FORBIDDEN", str(exc)) from exc


async def get_tenant_id(principal: Principal = Depends(get_principal)) -> str:
    """当前请求的租户：取自已验证的凭证，覆盖请求体中可能携带的租户字段。"""
    return principal.tenant_id


def require_scope(scope: str) -> Callable[..., Awaitable[Principal]]:
    """构造要求调用主体持有 ``scope`` 的依赖；缺少时返回 403。"""

    async def _require(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has_scope(scope):
            raise ApiError(403, "FORBIDDEN", f"credentials lack the '{scope}' scope")
        return principal

    return _require


#: 读接口（GET）所需 scope。
require_read = require_scope(SCOPE_READ)
#: 写接口（创建、删除、启动工作流、请求决策）所需 scope。
require_write = require_scope(SCOPE_WRITE)
