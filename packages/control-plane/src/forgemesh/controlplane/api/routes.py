"""REST API 路由：资源 CRUD + 状态查询 + 运行时端点。

对齐 ``api/openapi/openapi.yaml`` 契约，以 ``/api/v1`` 为前缀：

* ``POST   /inference-services``          —— 创建推理服务
* ``GET    /inference-services``          —— 列出推理服务（``limit`` 条一页，``pageToken`` 翻页）
* ``GET    /inference-services/{name}``   —— 获取单个推理服务
* ``PUT    /inference-services/{name}``   —— 整体替换 spec / labels / annotations
* ``PATCH  /inference-services/{name}``   —— JSON Merge Patch（RFC 7386）更新，规则同 PUT
  （见 :mod:`.updates`；单个资源的响应带 ``ETag``，``If-Match`` 不匹配返回 409）
* ``DELETE /inference-services/{name}``   —— 删除推理服务（受控删除：无
  finalizer 且没有加速器分配 → 204 物理删除；有 finalizer 或分配 → 202
  ``Terminating``，协调器驱动受控释放后物理删除，见 development-plan 第 7 章）
* ``POST   /workflows``                   —— 启动工作流运行（development-plan 5.4）
* ``POST   /decisions``                   —— 结构化决策（决策≠授权，development-plan 5.5）
* ``GET    /resources``                   —— 通用资源列表（按 kind 过滤，分页同上）

租户取自已验证的凭证（见 :func:`.dependencies.get_principal` 与
:mod:`forgemesh.controlplane.auth`），作为多租户隔离的权威来源，覆盖请求体中的
租户字段；读接口要求 ``read`` scope，写接口要求 ``write`` scope。

运行时端点（workflows / decisions）依赖注入的 ``WorkflowRuntime`` /
``DecisionRouter``（见 :func:`.dependencies.init_workflow_runtime` 等）；
错误统一使用 ``{"error": {"code", "message", "correlationId"}}`` 信封
（对齐 OpenAPI ``Error`` schema，见 :func:`forgemesh.controlplane.app.error_envelope`）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import Resource
from forgemesh.decision.provider import DecisionRequest as InternalDecisionRequest
from forgemesh.resource.inference import InferenceService
from forgemesh.workflow.runtime import WorkflowDefinition
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..app import error_envelope
from ..registry import ConflictReason, InvalidPageTokenError, SQLiteRegistry
from .dependencies import (
    ComponentNotReadyError,
    get_decision_router,
    get_tenant_id,
    get_workflow_runtime,
    require_read,
    require_registry,
    require_write,
)
from .errors import ApiError, validation_details
from .updates import (
    MERGE_PATCH_MEDIA_TYPE,
    SERVER_OWNED_METADATA,
    apply_merge_patch,
    check_update,
    conflict,
    etag_of,
    parse_if_match,
    unknown_fields,
)

router = APIRouter(prefix="/api/v1")

_KIND = "InferenceService"

#: 新建资源的 ``metadata.generation``（``observedGeneration`` 为 0：控制器尚未处理）。
INITIAL_GENERATION = 1

#: 列表接口单页条数上限。
MAX_LIST_LIMIT = 1000

#: PUT / PATCH 在读取与写入之间遇到并发写入（如控制器写 status）时重新读取并重试，
#: 这是总尝试次数。
_UPDATE_ATTEMPTS = 5


def _build_inference_service(body: dict[str, Any], tenant_id: str) -> InferenceService:
    """从请求体构造强类型 :class:`InferenceService`（OpenAPI 要求必须携带 ``spec``）。

    租户取自已验证的凭证，``uid`` 由服务端分配，``generation`` 从
    :data:`INITIAL_GENERATION` 开始；``status`` / ``conditions`` / ``finalizers`` 由控制
    平面维护，创建请求中的这些字段与 generation 字段被忽略。

    Raises:
        ValueError: 含未知的顶层字段，``kind`` 不是 ``InferenceService``，或 ``metadata``
            不是对象。
        pydantic.ValidationError: ``metadata`` / ``spec`` 不满足模型约束。
    """
    if unknown := unknown_fields(body):
        raise ValueError(f"unknown top-level fields: {unknown}")
    kind = body.get("kind", "InferenceService")
    if kind != "InferenceService":
        raise ValueError(f"kind must be 'InferenceService' on this endpoint, got {kind!r}")
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    metadata = {k: v for k, v in metadata.items() if k not in SERVER_OWNED_METADATA}
    metadata["tenantId"] = tenant_id
    metadata["uid"] = uuid4().hex
    metadata["generation"] = INITIAL_GENERATION
    return InferenceService.model_validate(
        {
            "apiVersion": body.get("apiVersion", "forgemesh.io/v1alpha1"),
            "kind": "InferenceService",
            "metadata": metadata,
            "spec": body.get("spec"),
        }
    )


def _invalid_limit(limit: int) -> JSONResponse | None:
    if 1 <= limit <= MAX_LIST_LIMIT:
        return None
    return _error_response(
        400,
        "BAD_REQUEST",
        f"limit must be between 1 and {MAX_LIST_LIMIT}, got {limit}",
        uuid4().hex,
    )


async def _list_response(
    registry: SQLiteRegistry,
    kind: str,
    tenant_id: str,
    namespace: str | None,
    page_token: str | None,
    limit: int,
) -> dict[str, Any]:
    """列表响应 ``{"items", "count", "nextPageToken"}``；还有更多结果时才带 ``nextPageToken``。

    空的 ``pageToken`` 等同于省略（第一页）。

    Raises:
        ApiError: ``pageToken`` 无效，或签发给了另一个租户或另一组过滤条件（400）。
    """
    try:
        page = await registry.list_page(
            kind, tenant_id, namespace=namespace, page_token=page_token or None, limit=limit
        )
    except InvalidPageTokenError as exc:
        raise ApiError(
            400,
            "BAD_REQUEST",
            "invalid pageToken: it is malformed, was modified, or was issued for "
            "a different tenant or filter set",
        ) from exc
    body: dict[str, Any] = {
        "items": [resource.model_dump(by_alias=True, mode="json") for resource in page.items],
        "count": len(page.items),
    }
    if page.next_page_token is not None:
        body["nextPageToken"] = page.next_page_token
    return body


def _resource_response(resource: Resource, status_code: int = 200) -> JSONResponse:
    """单个资源的 JSON 表示，附带当前版本的 ``ETag``。"""
    return JSONResponse(
        status_code=status_code,
        content=resource.model_dump(by_alias=True, mode="json"),
        headers={"ETag": etag_of(resource)},
    )


# ------------------------------------------------------------------ #
# 推理服务 CRUD
# ------------------------------------------------------------------ #


@router.post("/inference-services", status_code=201, dependencies=[Depends(require_write)])
async def create_inference_service(
    body: dict[str, Any],
    tenant_id: str = Depends(get_tenant_id),
    registry: SQLiteRegistry = Depends(require_registry),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
) -> Any:
    """创建推理服务（201，附 ``ETag``）。

    请求体不合法（缺少 ``spec``、kind 不匹配、未知的顶层字段等）返回 400；同名资源已存在
    返回 409。
    """
    correlation_id = x_correlation_id or uuid4().hex
    try:
        resource = _build_inference_service(body, tenant_id)
    except ValidationError as exc:
        return _error_response(
            400,
            "BAD_REQUEST",
            "推理服务创建请求校验失败",
            correlation_id,
            {"errors": validation_details(exc)},
        )
    except ValueError as exc:
        return _error_response(400, "BAD_REQUEST", str(exc), correlation_id)
    try:
        created = await registry.put(resource)
    except ConflictError as exc:
        return _error_response(409, "CONFLICT", str(exc), correlation_id)
    return _resource_response(created, status_code=201)


@router.get("/inference-services", dependencies=[Depends(require_read)])
async def list_inference_services(
    tenant_id: str = Depends(get_tenant_id),
    registry: SQLiteRegistry = Depends(require_registry),
    namespace: str | None = None,
    limit: int = 100,
    page_token: str | None = Query(default=None, alias="pageToken"),
) -> Any:
    """列出推理服务（可选按 namespace 过滤），按 ``(namespace, name)`` 升序分页。

    响应带 ``nextPageToken`` 时还有下一页：以相同的 ``namespace`` 把它作为 ``pageToken``
    传回（``limit`` 可以不同）；无效、被篡改或签发给另一租户 / 另一组过滤条件的
    ``pageToken`` 返回 400。
    """
    if (invalid := _invalid_limit(limit)) is not None:
        return invalid
    return await _list_response(registry, _KIND, tenant_id, namespace, page_token, limit)


@router.get("/inference-services/{name}", dependencies=[Depends(require_read)])
async def get_inference_service(
    name: str,
    tenant_id: str = Depends(get_tenant_id),
    registry: SQLiteRegistry = Depends(require_registry),
    namespace: str | None = None,
) -> JSONResponse:
    """获取单个推理服务（附 ``ETag``）；不存在时返回 HTTP 404。"""
    try:
        resource = await registry.get("InferenceService", namespace, name, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"InferenceService '{name}' not found") from exc
    return _resource_response(resource)


async def _update_inference_service(
    registry: SQLiteRegistry,
    tenant_id: str,
    name: str,
    namespace: str | None,
    if_match: list[str] | None,
    document_for: Callable[[dict[str, Any]], Any],
) -> JSONResponse:
    """PUT / PATCH 的共同流程：读取当前资源 → 生成更新文档 → 检查 → CAS 写入。

    ``document_for`` 由当前资源的 JSON 表示得到更新文档。客户端的前置条件每一轮都对照
    这一轮读到的资源检查，写入以读到的 uid 与版本为条件；读取之后资源被并发修改（如
    控制器写入 status）时重新读取再来一轮，不会覆盖并发写入，也不会绕过前置条件。
    """
    condition = parse_if_match(if_match)
    for _ in range(_UPDATE_ATTEMPTS):
        try:
            current, phase = await registry.get_with_phase(_KIND, namespace, name, tenant_id)
        except NotFoundError as exc:
            raise ApiError(404, "NOT_FOUND", f"InferenceService '{name}' not found") from exc
        if phase == "Terminating":
            raise conflict(
                f"InferenceService '{name}' is terminating and no longer accepts updates",
                ConflictReason.TERMINATING,
            )
        document = document_for(current.model_dump(by_alias=True, mode="json"))
        desired = check_update(document, current, tenant_id=tenant_id, if_match=condition)
        try:
            updated = await registry.update_spec(
                _KIND,
                namespace,
                name,
                tenant_id,
                desired.spec,
                labels=desired.labels,
                annotations=desired.annotations,
                expected_version=current.metadata.resourceVersion,
                expected_uid=current.metadata.uid,
            )
        except NotFoundError as exc:
            raise ApiError(404, "NOT_FOUND", f"InferenceService '{name}' not found") from exc
        except ConflictError as exc:
            if (exc.details or {}).get("reason") == ConflictReason.TERMINATING:
                raise conflict(exc.message, ConflictReason.TERMINATING) from exc
            continue
        return _resource_response(updated)
    raise conflict(
        f"InferenceService '{name}' kept changing while the update was applied; retry",
        ConflictReason.RESOURCE_VERSION,
    )


@router.put("/inference-services/{name}", dependencies=[Depends(require_write)])
async def replace_inference_service(
    name: str,
    body: dict[str, Any],
    tenant_id: str = Depends(get_tenant_id),
    registry: SQLiteRegistry = Depends(require_registry),
    namespace: str | None = None,
    if_match: list[str] | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """整体替换推理服务的 ``spec``、labels 与 annotations（200，附 ``ETag``）。

    文档规则见 :mod:`.updates`：只有 ``spec`` 变化时 ``generation`` 递增，没有变化的请求
    不写库。错误：文档不合法或试图修改不可变字段 400；不存在 404；资源处于 Terminating，
    或 uid / resourceVersion / If-Match 前置条件不满足 409（``details.reason``）。
    """
    return await _update_inference_service(
        registry, tenant_id, name, namespace, if_match, lambda _current: body
    )


@router.patch("/inference-services/{name}", dependencies=[Depends(require_write)])
async def patch_inference_service(
    name: str,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    registry: SQLiteRegistry = Depends(require_registry),
    namespace: str | None = None,
    if_match: list[str] | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """以 JSON Merge Patch（RFC 7386）更新推理服务（200，附 ``ETag``）。

    补丁合并到当前资源的 JSON 表示（与 GET 的响应相同）之后，按与 PUT 相同的规则检查
    并写入。错误：媒体类型不是 ``application/merge-patch+json`` 415；补丁不是 JSON 对象
    400；其余同 PUT。
    """
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != MERGE_PATCH_MEDIA_TYPE:
        raise ApiError(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            f"PATCH requires Content-Type: {MERGE_PATCH_MEDIA_TYPE}",
            headers={"Accept-Patch": MERGE_PATCH_MEDIA_TYPE},
        )
    try:
        patch = json.loads(await request.body())
    except ValueError as exc:
        raise ApiError(400, "BAD_REQUEST", "the merge patch is not valid JSON") from exc
    if not isinstance(patch, dict):
        raise ApiError(400, "BAD_REQUEST", "the merge patch must be a JSON object")
    return await _update_inference_service(
        registry,
        tenant_id,
        name,
        namespace,
        if_match,
        lambda current: apply_merge_patch(current, patch),
    )


@router.delete("/inference-services/{name}", dependencies=[Depends(require_write)])
async def delete_inference_service(
    name: str,
    tenant_id: str = Depends(get_tenant_id),
    registry: SQLiteRegistry = Depends(require_registry),
    namespace: str | None = None,
) -> Any:
    """删除推理服务（受控删除语义，对齐 development-plan 第 7 章除闭环）。

    * 无 finalizer、无外部副作用（如 Pending/Failed）且没有 Active 加速器分配
      → **204** 直接物理删除；
    * 有 finalizer（Ready 等有副作用态、或持有 Active 分配时自动追加默认
      finalizer）→ **202**，资源置为 ``Terminating``，由协调器驱动「受控释放 GPU
      → 清空 finalizer → 释放分配 → 物理删除」；响应体
      ``{"name": ..., "phase": "Terminating"}``。

    不存在时返回 HTTP 404（不变）。
    """
    try:
        result = await registry.request_delete("InferenceService", namespace, name, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"InferenceService '{name}' not found") from exc
    except ConflictError as exc:
        return _error_response(409, "CONFLICT", str(exc), uuid4().hex)
    if result.deleted:
        return Response(status_code=204)
    return JSONResponse(status_code=202, content={"name": name, "phase": "Terminating"})


# ------------------------------------------------------------------ #
# 通用资源端点
# ------------------------------------------------------------------ #


@router.get("/resources", dependencies=[Depends(require_read)])
async def list_resources(
    tenant_id: str = Depends(get_tenant_id),
    registry: SQLiteRegistry = Depends(require_registry),
    kind: str | None = None,
    namespace: str | None = None,
    limit: int = 100,
    page_token: str | None = Query(default=None, alias="pageToken"),
) -> Any:
    """列出某个 kind 的资源（可选按 namespace 过滤），分页规则同推理服务列表。

    未指定 ``kind`` 时返回空列表（P1 简化：避免全表扫描）；这时不会签发 ``nextPageToken``，
    带 ``pageToken`` 返回 400。
    """
    if (invalid := _invalid_limit(limit)) is not None:
        return invalid
    if not kind:
        if page_token:
            raise ApiError(
                400, "BAD_REQUEST", "invalid pageToken: a listing without kind has no further pages"
            )
        return {"items": [], "count": 0}
    return await _list_response(registry, kind, tenant_id, namespace, page_token, limit)


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


@router.post("/workflows", status_code=202, dependencies=[Depends(require_write)])
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
            {"errors": validation_details(exc)},
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

    OpenAPI 契约未含租户字段，租户一律取自已验证的凭证。

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


@router.post("/decisions", dependencies=[Depends(require_write)])
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
            {"errors": validation_details(exc)},
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
