"""幂等去重键生成。

对齐 development-plan 5.4：dedup_key 由 kind/namespace/name/uid/generation/operation 派生，
同一键的协调不得重复产生副作用。
"""

from __future__ import annotations

import hashlib
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DedupKey(BaseModel):
    """幂等去重键。

    Attributes:
        key: SHA-256 摘要（去重比对依据）。
        resource_kind: 资源类型（审计/诊断用）。
        resource_name: 资源名（审计/诊断用）。
        generation: 期望状态版本（审计/诊断用）。
        operation: 操作语义标识（审计/诊断用）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    resource_kind: str
    resource_name: str
    generation: int
    operation: str


def compute_dedup_key(
    kind: str,
    namespace: Optional[str],
    name: str,
    uid: Optional[str],
    generation: int,
    operation: str,
) -> DedupKey:
    """计算幂等去重键（SHA-256 摘要）。

    将 ``kind|namespace|name|uid|generation|operation`` 拼接为原始串，
    取 UTF-8 编码的 SHA-256 十六进制摘要作为 ``key``。相同输入必然产生
    相同 ``key``；任一字段不同则 ``key`` 不同（抗碰撞）。

    Args:
        kind: 资源类型标识。
        namespace: 命名空间（可为 ``None``）。
        name: 资源名。
        uid: 资源稳定标识（可为 ``None``）。
        generation: 期望状态版本。
        operation: 操作语义标识。

    Returns:
        :class:`DedupKey` 实例。
    """
    raw = f"{kind}|{namespace or ''}|{name}|{uid or ''}|{generation}|{operation}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return DedupKey(
        key=digest,
        resource_kind=kind,
        resource_name=name,
        generation=generation,
        operation=operation,
    )


__all__ = ["DedupKey", "compute_dedup_key"]
