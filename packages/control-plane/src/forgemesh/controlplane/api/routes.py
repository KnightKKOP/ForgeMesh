"""REST API 路由：资源 CRUD + 状态查询 + 运行时端点。

对齐 ``api/openapi/openapi.yaml`` 契约，以 ``/api/v1`` 为前缀：

* ``POST   /inference-services``          —— 创建推理服务
* ``GET    /inference-services``          —— 列出推理服务
* ``GET    /inference-services/{name}``   —— 获取单个推理服务
* ``DELETE /inference-services/{name}``   —— 删除推理服务
* ``POST   /workflows``                   —— 启动工作流运行（development-plan 5.4）
* ``POST   /decisions``                   —— 结构化决策（决策≠授权，development-plan 5.5）
* ``GET    /resources``                   —— 通用资源列表（可选 kind 过滤）

租户上下文通过 ``X-Tenant-Id`` 请求头注入（见 :mod:`.dependencies`），
作为多租户隔离的权威来源，覆盖请求体中的租户字段。

运行时端点（workflows / decisions）依赖注入的 ``WorkflowRuntime`` /
``DecisionRouter``（见 :func:`.dependencies.init_workflow_runtime` 等）；
错误统一使用 ``{"error": {"code", "message", "correlationId"}}`` 信封
（对齐 OpenAPI ``Error`` schema，见 :func:`forgemesh.controlplane.app.error_envelope`）。

兼容 Python 3.9+。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.decision.provider import DecisionRequest as InternalDecisionRequest
from forgemesh.workflow.runtime import WorkflowDefinition
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..app import error_envelope
from .dependencies import (
    ComponentNotReadyError,
    get_decision_router,
    get_registry,
    get_tenant_id,
    get_workflow_runtime,
)

router = APIRouter(prefix="/api/v1")


class ResourceCreateRequest(BaseModel):
    """资源创建请求体。

    Attributes:
        apiVersion: 资源组/版本，默认 ``forgemesh.io/v1alpha1``。
        kind: 资源类型（如 ``InferenceService``）。
        metadata: 资源元数据（name/namespace/labels 等）。
        spec: 期望状态（P1 基类 ``Resource`` 暂不持久化，后续强类型子类扩展）。
        status: 观测状态（同上）。
    """

    apiVersion: str = "forgemesh.io/v1alpha1"
    kind: str
    metadata: dict[str, Any]
    spec: dict[str, Any] | None = None
    status: dict[str, Any] | None = None


class ResourceListResponse(BaseModel):
    """资源列表响应。"""

    items: list[dict[str, Any]]
    count: int


def _build_resource(body: ResourceCreateRequest, tenant_id: str) -> Resource:
    """从请求体构造 :class:`Resource`，注入租户上下文。

    请求头 ``X-Tenant-Id`` 为租户权威来源，覆盖请求体中可能存在的
    ``tenant_id`` / ``tenantId`` 字段，防止跨租户伪造。

    P1 简化：基类 ``Resource`` 配置 ``extra="forbid"``，不携带 spec/status；
    强类型子类（如 ``InferenceService``）在后续阶段扩展。
    """
    meta_dict = dict(body.metadata)
    meta_dict.pop("tenant_id", None)
    meta_dict.pop("tenantId", None)
    metadata = ObjectMeta(**meta_dict, tenant_id=tenant_id)
    return Resource(kind=body.kind, metadata=metadata)


# ------------------------------------------------------------------ #
# 推理服务 CRUD
# ------------------------------------------------------------------ #


@router.post("/inference-services")
async def create_inference_service(
    body: ResourceCreateRequest,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    """创建推理服务。

    同名资源已存在时返回 HTTP 409（ConflictError）。
    """
    registry = get_registry()
    resource = _build_resource(body, tenant_id)
    try:
        created = await registry.put(resource)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return created.model_dump(by_alias=True)


@router.get("/inference-services")
async def list_inference_services(
    tenant_id: str = Depends(get_tenant_id),
    namespace: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """列出推理服务（可选按 namespace 过滤、limit 分页）。"""
    registry = get_registry()
    items = await registry.list("InferenceService", tenant_id, namespace=namespace, limit=limit)
    return {"items": [r.model_dump(by_alias=True) for r in items], "count": len(items)}


@router.get("/inference-services/{name}")
async def get_inference_service(
    name: str,
    tenant_id: str = Depends(get_tenant_id),
    namespace: str | None = None,
) -> dict[str, Any]:
    """获取单个推理服务；不存在时返回 HTTP 404。"""
    registry = get_registry()
    try:
        resource = await registry.get("InferenceService", namespace, name, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"InferenceService '{name}' not found") from exc
    return resource.model_dump(by_alias=True)


@router.delete("/inference-services/{name}")
async def delete_inference_service(
    name: str,
    tenant_id: str = Depends(get_tenant_id),
    namespace: str | None = None,
) -> dict[str, Any]:
    """删除推理服务；不存在时返回 HTTP 404。"""
    registry = get_registry()
    try:
        await registry.delete("InferenceService", namespace, name, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"InferenceService '{name}' not found") from exc
    return {"status": "deleted"}


# ------------------------------------------------------------------ #
# 通用资源端点
# ------------------------------------------------------------------ #


@router.get("/resources")
async def list_resources(
    tenant_id: str = Depends(get_tenant_id),
    kind: str | None = None,
    namespace: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """列出资源（可选按 kind / namespace 过滤）。

    未指定 ``kind`` 时返回空列表（P1 简化：避免全表扫描）。
    """
    registry = get_registry()
    if kind:
        items = await registry.list(kind, tenant_id, namespace=namespace, limit=limit)
    else:
        items = []
    return {"items": [r.model_dump(by_alias=True) for r in items], "count": len(items)}


# ------------------------------------------------------------------ #
# 工作流运行端点（development-plan 5.4）
# ------------------------------------------------------------------ #


class WorkflowStartRequest(BaseModel):
    """工作流启动请求体（对齐 OpenAPI ``WorkflowRunRequest``）。

    Attributes:
        definition: WorkflowDefinition 名称。
        version: 期望的定义版本（不可变）；当前无定义仓库，仅做合法性校验，
            实际固定版本以 runtime 返回的 ``definition_version`` 为准。
        inputs: 运行时输入（覆盖/补充定义默认值）。
        idempotency_key: 调用方幂等键（OpenAPI 未声明的扩展字段）；
            缺省自动生成。API 层以 ``{tenant_id}:{key}`` 命名空间化，
            防止跨租户幂等碰撞。
    """

    model_config = ConfigDict(populate_by_name=True)

    definition: str = Field(min_length=1)
    version: int = Field(ge=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1)


class WorkflowRunResponse(BaseModel):
    """工作流运行响应体（对齐 OpenAPI ``WorkflowRun``）。"""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(alias="runId")
    state: str = "Pending"
    definition_version: int = Field(alias="definitionVersion")


def _validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    """提取校验错误摘要（loc/msg/type），剥离不可 JSON 序列化的 ``ctx``。"""
    return [
        {"loc": [str(x) for x in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]


def _error_response(
    status_code: int,
    code: str,
    message: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """构造统一错误信封响应（对齐 OpenAPI ``Error`` schema）。"""
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(code, message, correlation_id, details),
    )


@router.post("/workflows", status_code=202)
async def start_workflow(
    body: dict[str, Any],
    tenant_id: str = Depends(get_tenant_id),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
) -> Any:
    """启动工作流运行（202 已受理）。

    语义（对齐 development-plan 5.4 / OpenAPI ``startWorkflow``）：

    * 请求体以 OpenAPI ``WorkflowRunRequest`` 为准，校验失败返回 400 错误信封
      （而非框架默认 422，保持契约一致）；
    * 调用注入的 ``WorkflowRuntime.start(defn, inputs, idempotency_key)``；
      幂等键以租户为命名空间，同一键重复请求复用既有运行（不产生重复副作用）；
    * 响应体为 OpenAPI ``WorkflowRun``（``runId`` / ``state`` / ``definitionVersion``），
      受理初始状态为 ``Pending``。

    错误映射：校验失败 400；定义不存在 404（``NotFoundError``）；
    ``WorkflowRuntime`` 未注入 503；其余内部错误 500（统一错误信封）。
    """
    correlation_id = x_correlation_id or uuid4().hex
    try:
        req = WorkflowStartRequest.model_validate(body)
    except ValidationError as exc:
        return _error_response(
            400,
            "BAD_REQUEST",
            "工作流启动请求校验失败",
            correlation_id,
            {"errors": _validation_details(exc)},
        )

    try:
        runtime = get_workflow_runtime()
    except ComponentNotReadyError as exc:
        return _error_response(503, "WORKFLOW_RUNTIME_UNAVAILABLE", str(exc), correlation_id)

    # 无定义仓库（阶段 0）：按名称构造最小定义，版本固定语义由 runtime 负责。
    defn = WorkflowDefinition(name=req.definition, inputs=dict(req.inputs), steps=[])
    idempotency_key = f"{tenant_id}:{req.idempotency_key or uuid4().hex}"
    try:
        run_ref = await runtime.start(defn, dict(req.inputs), idempotency_key)
    except NotFoundError as exc:
        return _error_response(
            404,
            "NOT_FOUND",
            f"工作流定义 '{req.definition}' 不存在: {exc}",
            correlation_id,
        )
    except Exception as exc:  # noqa: BLE001 - runtime 任意故障统一转错误信封
        return _error_response(500, "INTERNAL_ERROR", f"启动工作流运行失败: {exc}", correlation_id)

    response = WorkflowRunResponse(
        run_id=run_ref.run_id, definition_version=run_ref.definition_version
    )
    return response.model_dump(by_alias=True)


# ------------------------------------------------------------------ #
# 决策端点（development-plan 5.5：决策 ≠ 授权，绝不默认放行）
# ------------------------------------------------------------------ #


class DecisionCallRequest(BaseModel):
    """决策请求体（对齐 OpenAPI ``DecisionRequest``）。

    OpenAPI 契约未含租户字段，租户一律由 ``X-Tenant-Id`` 请求头注入。

    Attributes:
        question: 决策问题；API 层映射为内部模型的 ``operation`` 语义标识。
        context: 决策上下文。
        required_capabilities: 期望的 Provider 能力列表；并入内部 ``context``
            传递（内部模型 ``extra="forbid"``，无对应字段）。
    """

    model_config = ConfigDict(populate_by_name=True)

    question: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] | None = Field(default=None, alias="requiredCapabilities")


@router.post("/decisions")
async def request_decision(
    body: dict[str, Any],
    tenant_id: str = Depends(get_tenant_id),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
) -> Any:
    """请求一次结构化决策（决策≠授权；授权由 Scheduler/Policy 完成）。

    语义（对齐 development-plan 5.5 / OpenAPI ``requestDecision``）：

    * 请求体以 OpenAPI ``DecisionRequest`` 为 API 层形状，内部转换为
      ``forgemesh.decision.DecisionRequest``（租户取自请求头）；
    * 调用注入的 ``DecisionRouter.route``，按序降级直至产出有效候选；
    * 响应体为 OpenAPI ``DecisionResponse``（``decisionId`` / ``outcome`` /
      ``rationale`` / ``trace`` / ``degraded``），``trace.attempts`` 提供降级
      路径摘要（可解释性）。

    错误映射：校验失败 400；全部 Provider 失败 503（**绝不默认放行**，
    ``DecisionResult.success=False`` 交人工处理）；``DecisionRouter`` 未注入
    503；其余内部错误 500（统一错误信封）。
    """
    correlation_id = x_correlation_id or uuid4().hex
    try:
        req = DecisionCallRequest.model_validate(body)
    except ValidationError as exc:
        return _error_response(
            400,
            "BAD_REQUEST",
            "决策请求校验失败",
            correlation_id,
            {"errors": _validation_details(exc)},
        )

    try:
        decision_router = get_decision_router()
    except ComponentNotReadyError as exc:
        return _error_response(503, "DECISION_ROUTER_UNAVAILABLE", str(exc), correlation_id)

    # OpenAPI 形状 → 内部模型：question→operation，能力列表并入 context。
    request_id = f"dec-{uuid4().hex}"
    context = dict(req.context)
    if req.required_capabilities is not None:
        context["requiredCapabilities"] = list(req.required_capabilities)
    internal = InternalDecisionRequest(
        request_id=request_id,
        tenant_id=tenant_id,
        operation=req.question,
        context=context,
    )

    try:
        result = await decision_router.route(internal)
    except Exception as exc:  # noqa: BLE001 - Router 任意故障统一转错误信封
        return _error_response(500, "INTERNAL_ERROR", f"决策路由失败: {exc}", correlation_id)

    if not result.success or result.chosen is None:
        # 全部 Provider 失败：绝不默认放行，503 交人工处理（5.5 降级决策表）。
        return _error_response(
            503,
            "DECISION_NO_CANDIDATE",
            result.error or "所有决策 Provider 均未返回有效候选（禁止默认放行）",
            correlation_id,
            {"attempts": [a.model_dump() for a in result.attempts]},
        )

    chosen = result.chosen
    rationale = "; ".join(chosen.rationale) or f"首个有效候选（action={chosen.action}）"
    return {
        "decisionId": request_id,
        "outcome": chosen.action,
        "rationale": rationale,
        "trace": {
            "requestId": request_id,
            "attempts": [a.model_dump() for a in result.attempts],
            "candidates": [c.model_dump() for c in result.candidates],
        },
        "degraded": any(not a.ok for a in result.attempts),
    }


def create_api_router() -> APIRouter:
    """创建 API 路由器。

    使用方（如 ``app.include_router(create_api_router())``）负责挂载到
    FastAPI 应用实例。
    """
    return router
