"""计量基础插件 Provider（对齐 development-plan WP-4.3）。

基于已实现的 :mod:`plugins.metering_basic.usage` 模块，封装为可插拔的
计量 Provider，供插件运行时通过统一接口记录与查询使用量。

设计约束：

* **薄封装**：Provider 仅转发调用至 :class:`UsageStore`，核心追加式语义、
  多租户隔离、异步线程池执行等逻辑均由 ``UsageStore`` 承担。
* **追加式不可变**（IVT-10）：记录只插入不更新；计量修正通过新增
  ``correction_of`` 记录实现，原记录始终保留，保证审计可追溯。
"""

from __future__ import annotations

from .usage import UsageRecord, UsageStore, UsageSummary


class MeteringProvider:
    """计量 Provider：封装 :class:`UsageStore` 为可插拔组件。

    Args:
        store: 追加式使用记录存储实例。
    """

    def __init__(self, store: UsageStore) -> None:
        self._store = store

    @property
    def store(self) -> UsageStore:
        """暴露内部 :class:`UsageStore`，供需要直接访问的调用方使用。"""
        return self._store

    async def record_usage(self, record: UsageRecord) -> None:
        """记录一次使用（追加式，不可修改已有记录）。

        Args:
            record: 使用记录。

        Raises:
            ConflictError: ``record_id`` 已存在（追加式约束）。
        """
        await self._store.record(record)

    async def get_summary(self, tenant_id: str, start: float, end: float) -> UsageSummary:
        """获取指定租户在 ``[start, end)`` 时间范围内的聚合统计。

        Args:
            tenant_id: 租户标识。
            start: 起始时间戳（包含）。
            end: 结束时间戳（不包含）。

        Returns:
            :class:`UsageSummary`；无记录时返回各字段为零的空统计。
        """
        return await self._store.aggregate(tenant_id, start, end)

    async def correct_usage(self, original_id: str, correction: UsageRecord) -> None:
        """修正计量：新增一条 ``correction_of`` 记录指向原记录，原记录保留（IVT-10）。

        Args:
            original_id: 原记录 ID。
            correction: 修正记录（使用自身 ``record_id``，须唯一）。

        Raises:
            NotFoundError: 原记录不存在。
            ConflictError: 修正记录的 ``record_id`` 已存在。
        """
        await self._store.correct(original_id, correction)


__all__ = [
    "MeteringProvider",
]
