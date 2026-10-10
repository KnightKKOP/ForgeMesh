"""Registry 写入前 Schema 校验与 generation 语义。

提供资源准入校验（``apiVersion`` / ``kind`` / ``metadata.name`` 非空）与
spec 变更时的 generation 自增（委托 :meth:`Resource.with_generation_bump`，
保持不可变派生语义）。

兼容 Python 3.9+。
"""

from __future__ import annotations

from forgemesh.core.errors import InvalidStateTransition
from forgemesh.core.objects import Resource


def validate_resource(resource: Resource) -> None:
    """写入前校验：``apiVersion`` / ``kind`` / ``metadata.name`` 非空。

    Raises:
        InvalidStateTransition: 任一必填字段为空。
    """
    if not resource.apiVersion:
        raise InvalidStateTransition("apiVersion must not be empty")
    if not resource.kind:
        raise InvalidStateTransition("kind must not be empty")
    if not resource.metadata.name:
        raise InvalidStateTransition("metadata.name must not be empty")


def bump_generation(resource: Resource) -> Resource:
    """spec 变更时 generation 自增（返回副本，不修改原对象）。"""
    return resource.with_generation_bump()


__all__ = ["validate_resource", "bump_generation"]
