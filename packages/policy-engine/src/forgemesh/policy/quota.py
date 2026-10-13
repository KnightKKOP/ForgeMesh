"""多租户配额策略模型与评估（对齐 development-plan 5.6 / architecture 4.1）。

提供租户级资源配额的检查、消费、释放与上限管理。所有变更操作
（consume / release / set_limit）通过 :class:`asyncio.Lock` 串行化，
保证原子性——并发场景下不会超额分配。

设计要点：

* 以 ``"{tenant_id}:{resource_type}"`` 为键存储 :class:`QuotaLimit`，强制多租户隔离。
* :meth:`QuotaPolicy.consume` 在锁内完成「检查 + 递增」，避免 check-then-act 竞态。
* :meth:`QuotaPolicy.get_usage` 返回副本，防止外部修改内部状态。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field


class QuotaLimit(BaseModel):
    """配额限制：单个租户对某类资源的容量上限与当前用量。

    Attributes:
        resource_type: 资源类型（如 ``InferenceService``）。
        max: 最大允许数量（>= 0）。
        current: 当前已用数量（>= 0）。
        tenant_id: 租户标识（全边界强制隔离）。
    """

    model_config = ConfigDict(extra="forbid")

    resource_type: str
    max: int = Field(ge=0)
    current: int = Field(default=0, ge=0)
    tenant_id: str


class QuotaDecision(BaseModel):
    """配额决策结果。

    Attributes:
        allowed: 是否允许本次请求。
        reason: 决策理由——``"quota_ok"`` / ``"quota_exceeded"`` / ``"no_quota_limit"``。
        current: 决策时的当前用量。
        max: 配额上限。
        requested: 本次请求的数量。
    """

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    current: int
    max: int
    requested: int


class QuotaPolicy:
    """配额策略：多租户资源配额的检查与消费。

    内部以 ``"{tenant_id}:{resource_type}"`` 为键存储 :class:`QuotaLimit`，
    保证多租户隔离。所有变更操作通过同一把 :class:`asyncio.Lock` 串行化，
    确保 consume / release / set_limit 的原子性。

    Args:
        limits: 初始配额限制映射；键为 ``"{tenant_id}:{resource_type}"``。
    """

    def __init__(self, limits: dict[str, QuotaLimit] | None = None) -> None:
        self._limits: dict[str, QuotaLimit] = dict(limits or {})
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(tenant_id: str, resource_type: str) -> str:
        """构造 ``"{tenant_id}:{resource_type}"`` 复合键。"""
        return f"{tenant_id}:{resource_type}"

    async def check(
        self,
        tenant_id: str,
        resource_type: str,
        requested: int = 1,
    ) -> QuotaDecision:
        """检查是否超配额（只读，不修改用量）。

        Args:
            tenant_id: 租户标识。
            resource_type: 资源类型。
            requested: 本次请求数量。

        Returns:
            :class:`QuotaDecision`。超配额时 ``allowed=False``、
            ``reason="quota_exceeded"``；无配额记录时默认放行。
        """
        limit = self._limits.get(self._key(tenant_id, resource_type))
        if limit is None:
            return QuotaDecision(
                allowed=True,
                reason="no_quota_limit",
                current=0,
                max=0,
                requested=requested,
            )
        if limit.current + requested > limit.max:
            return QuotaDecision(
                allowed=False,
                reason="quota_exceeded",
                current=limit.current,
                max=limit.max,
                requested=requested,
            )
        return QuotaDecision(
            allowed=True,
            reason="quota_ok",
            current=limit.current,
            max=limit.max,
            requested=requested,
        )

    async def consume(
        self,
        tenant_id: str,
        resource_type: str,
        amount: int = 1,
    ) -> QuotaDecision:
        """消费配额（原子操作，并发安全）。

        在锁保护下完成「检查 + 递增」；若超额则不修改用量并返回拒绝，
        保证并发多次消费不会超过上限。

        Args:
            tenant_id: 租户标识。
            resource_type: 资源类型。
            amount: 消费数量。

        Returns:
            :class:`QuotaDecision`。
        """
        async with self._lock:
            limit = self._limits.get(self._key(tenant_id, resource_type))
            if limit is None:
                return QuotaDecision(
                    allowed=True,
                    reason="no_quota_limit",
                    current=0,
                    max=0,
                    requested=amount,
                )
            if limit.current + amount > limit.max:
                return QuotaDecision(
                    allowed=False,
                    reason="quota_exceeded",
                    current=limit.current,
                    max=limit.max,
                    requested=amount,
                )
            limit.current += amount
            return QuotaDecision(
                allowed=True,
                reason="quota_ok",
                current=limit.current,
                max=limit.max,
                requested=amount,
            )

    async def release(
        self,
        tenant_id: str,
        resource_type: str,
        amount: int = 1,
    ) -> None:
        """释放配额（原子操作，并发安全）。

        递减 ``current``，下限为 0。无配额记录时静默忽略。

        Args:
            tenant_id: 租户标识。
            resource_type: 资源类型。
            amount: 释放数量。
        """
        async with self._lock:
            limit = self._limits.get(self._key(tenant_id, resource_type))
            if limit is None:
                return
            limit.current = max(0, limit.current - amount)

    async def set_limit(
        self,
        tenant_id: str,
        resource_type: str,
        max: int,
    ) -> None:
        """设置配额上限（原子操作）。

        若该租户 + 资源类型尚无记录，则创建一条 ``current=0`` 的记录；
        若已有记录，仅更新上限，保留当前用量。

        Args:
            tenant_id: 租户标识。
            resource_type: 资源类型。
            max: 配额上限（>= 0）。
        """
        async with self._lock:
            key = self._key(tenant_id, resource_type)
            limit = self._limits.get(key)
            if limit is None:
                self._limits[key] = QuotaLimit(
                    resource_type=resource_type,
                    max=max,
                    current=0,
                    tenant_id=tenant_id,
                )
            else:
                limit.max = max

    async def get_usage(
        self,
        tenant_id: str,
        resource_type: str,
    ) -> QuotaLimit | None:
        """查询使用量。

        Args:
            tenant_id: 租户标识。
            resource_type: 资源类型。

        Returns:
            :class:`QuotaLimit` 副本；无记录时返回 ``None``。
        """
        limit = self._limits.get(self._key(tenant_id, resource_type))
        if limit is None:
            return None
        return limit.model_copy()


__all__ = [
    "QuotaLimit",
    "QuotaDecision",
    "QuotaPolicy",
]
