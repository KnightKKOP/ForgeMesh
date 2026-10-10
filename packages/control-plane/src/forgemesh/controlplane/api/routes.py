"""REST API 路由：资源 CRUD + 状态查询 + 任务管理。

对齐 ``api/openapi/openapi.yaml`` 契约，以 ``/api/v1`` 为前缀：

* ``POST   /inference-services``          —— 创建推理服务
* ``GET    /inference-services``          —— 列出推理服务
* ``GET    /inference-services/{name}``   —— 获取单个推理服务
* ``DELETE /inference-services/{name}``   —— 删除推理服务
* ``GET    /resources``                   —— 通用资源列表（可选 kind 过滤）

租户上下文通过 ``X-Tenant-Id`` 请求头注入（见 :mod:`.dependencies`），
作为多租户隔离的权威来源，覆盖请求体中的租户字段。

兼容 Python 3.9+。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from pydantic import BaseModel

from .dependencies import get_registry, get_tenant_id

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
    metadata: Dict[str, Any]
    spec: Optional[Dict[str, Any]] = None
    status: Optional[Dict[str, Any]] = None


class ResourceListResponse(BaseModel):
    """资源列表响应。"""

    items: List[Dict[str, Any]]
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
) -> Dict[str, Any]:
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
    namespace: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """列出推理服务（可选按 namespace 过滤、limit 分页）。"""
    registry = get_registry()
    items = await registry.list(
        "InferenceService", tenant_id, namespace=namespace, limit=limit
    )
    return {"items": [r.model_dump(by_alias=True) for r in items], "count": len(items)}


@router.get("/inference-services/{name}")
async def get_inference_service(
    name: str,
    tenant_id: str = Depends(get_tenant_id),
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """获取单个推理服务；不存在时返回 HTTP 404。"""
    registry = get_registry()
    try:
        resource = await registry.get("InferenceService", namespace, name, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"InferenceService '{name}' not found"
        ) from exc
    return resource.model_dump(by_alias=True)


@router.delete("/inference-services/{name}")
async def delete_inference_service(
    name: str,
    tenant_id: str = Depends(get_tenant_id),
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    """删除推理服务；不存在时返回 HTTP 404。"""
    registry = get_registry()
    try:
        await registry.delete("InferenceService", namespace, name, tenant_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"InferenceService '{name}' not found"
        ) from exc
    return {"status": "deleted"}


# ------------------------------------------------------------------ #
# 通用资源端点
# ------------------------------------------------------------------ #


@router.get("/resources")
async def list_resources(
    tenant_id: str = Depends(get_tenant_id),
    kind: Optional[str] = None,
    namespace: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """列出资源（可选按 kind / namespace 过滤）。

    未指定 ``kind`` 时返回空列表（P1 简化：避免全表扫描）。
    """
    registry = get_registry()
    if kind:
        items = await registry.list(kind, tenant_id, namespace=namespace, limit=limit)
    else:
        items = []
    return {"items": [r.model_dump(by_alias=True) for r in items], "count": len(items)}


def create_api_router() -> APIRouter:
    """创建 API 路由器。

    使用方（如 ``app.include_router(create_api_router())``）负责挂载到
    FastAPI 应用实例。
    """
    return router
