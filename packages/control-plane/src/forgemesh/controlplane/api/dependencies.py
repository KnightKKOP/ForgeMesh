"""API 依赖注入：Registry 实例与租户上下文。"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException
from forgemesh.controlplane.registry import SQLiteRegistry

#: 全局 Registry 实例（由 ``init_registry`` 初始化）。
_registry: Optional[SQLiteRegistry] = None


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


async def get_tenant_id(
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> str:
    """从请求头 ``X-Tenant-Id`` 提取租户 ID（强制要求）。

    缺失时返回 HTTP 400。租户上下文为多租户隔离的权威来源，
    覆盖请求体中可能携带的租户字段。
    """
    if not x_tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-Id header is required")
    return x_tenant_id
