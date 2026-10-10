"""CLI 命令实现：直接调用 :class:`SQLiteRegistry`。

P1 简化：每个命令使用独立的内存 SQLite 库（``:memory:``），
不跨命令持久化。后续阶段将切换为 HTTP 客户端模式，通过 ``--api-url``
连接控制平面。

兼容 Python 3.9+。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource


def _get_registry() -> SQLiteRegistry:
    """获取 Registry 实例（CLI 使用本地 SQLite 内存库）。"""
    return SQLiteRegistry(":memory:")  # P1 简化：每次新建内存库


def _build_resource(data: dict[str, Any], tenant_id: str) -> Resource:
    """从 YAML 解析结果构造 :class:`Resource`，注入租户上下文。

    ``--tenant`` 为租户权威来源，覆盖 YAML metadata 中可能存在的
    ``tenant_id`` / ``tenantId`` 字段。
    """
    meta_dict = dict(data.get("metadata", {}))
    meta_dict.pop("tenant_id", None)
    meta_dict.pop("tenantId", None)
    meta = ObjectMeta(**meta_dict, tenant_id=tenant_id)
    return Resource(kind=data.get("kind", "InferenceService"), metadata=meta)


async def cmd_apply(args: Any) -> int:
    """``apply -f <file.yaml>``：创建/更新资源。"""
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        return 1

    with file_path.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    registry = _get_registry()
    resource = _build_resource(data, args.tenant)

    try:
        created = await registry.put(resource)
        print(
            f"Created: {created.kind}/{created.metadata.name} "
            f"(version={created.metadata.resourceVersion})"
        )
        return 0
    except ConflictError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


async def cmd_get(args: Any) -> int:
    """``get <kind> <name>``：查询资源。"""
    registry = _get_registry()
    try:
        resource = await registry.get(args.kind, args.namespace, args.name, args.tenant)
        print(
            json.dumps(
                resource.model_dump(by_alias=True), indent=2, ensure_ascii=False
            )
        )
        return 0
    except NotFoundError:
        print(f"Error: {args.kind}/{args.name} not found", file=sys.stderr)
        return 1


async def cmd_describe(args: Any) -> int:
    """``describe <kind> <name>``：查询资源详情（P1 等同 get）。"""
    return await cmd_get(args)


async def cmd_list(args: Any) -> int:
    """``list <kind>``：列出资源。"""
    registry = _get_registry()
    items = await registry.list(args.kind, args.tenant, namespace=args.namespace)
    for item in items:
        print(
            f"{item.kind}\t{item.metadata.namespace or 'default'}\t{item.metadata.name}"
        )
    return 0


async def cmd_delete(args: Any) -> int:
    """``delete <kind> <name>``：删除资源。"""
    registry = _get_registry()
    try:
        await registry.delete(args.kind, args.namespace, args.name, args.tenant)
        print(f"Deleted: {args.kind}/{args.name}")
        return 0
    except NotFoundError:
        print(f"Error: {args.kind}/{args.name} not found", file=sys.stderr)
        return 1
