"""审批等待与 commit 重校验。

实现审批等待机制：步骤标记 ``requires_approval`` 时进入 ``WAITING``，
等待外部信号后重校验。对齐 development-plan 5.4「审批」语义。

设计要点
--------
* **ApprovalGate**：用 :class:`asyncio.Event` 管理审批等待，支持通过/拒绝/超时。
* **CommitRevalidator**：审批通过后重校验输入/状态一致性，防止审批期间状态漂移
  导致盲目 commit（对齐 runtime.py docstring「commit 步骤重新校验」语义）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: commit 重校验函数类型：``(context) -> bool``，``True`` = 可 commit。
CommitValidator = Callable[[dict[str, Any]], bool]


@dataclass
class _ApprovalEntry:
    """单次审批的内部状态。"""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    #: ``None`` = 待决，``True`` = 通过，``False`` = 拒绝。
    approved: bool | None = None


class ApprovalGate:
    """审批门控：用 asyncio.Event 管理审批等待。

    每个 ``(run_id, step_id)`` 对应一个独立的审批请求。
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _ApprovalEntry] = {}

    def _get_or_create(self, run_id: str, step_id: str) -> _ApprovalEntry:
        """获取或创建审批条目。"""
        key = (run_id, step_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = _ApprovalEntry()
            self._entries[key] = entry
        return entry

    async def wait_for_approval(
        self,
        run_id: str,
        step_id: str,
        timeout: float | None = None,
    ) -> bool:
        """异步等待审批结果。

        Args:
            run_id: 运行实例标识。
            step_id: 步骤标识。
            timeout: 等待超时（秒）；``None`` 表示无限等待。

        Returns:
            ``True`` = 审批通过，``False`` = 审批拒绝或等待超时。
        """
        entry = self._get_or_create(run_id, step_id)

        # 已有结果（approve/reject 先于 wait 调用）。
        if entry.approved is not None:
            return entry.approved

        # 等待事件被 set。
        try:
            if timeout is not None:
                await asyncio.wait_for(entry.event.wait(), timeout=timeout)
            else:
                await entry.event.wait()
        except TimeoutError:
            return False

        return entry.approved or False

    def approve(self, run_id: str, step_id: str) -> None:
        """发送审批通过信号。"""
        entry = self._get_or_create(run_id, step_id)
        entry.approved = True
        entry.event.set()

    def reject(self, run_id: str, step_id: str) -> None:
        """发送审批拒绝信号。"""
        entry = self._get_or_create(run_id, step_id)
        entry.approved = False
        entry.event.set()

    def is_pending(self, run_id: str, step_id: str) -> bool:
        """检查指定步骤是否处于待审批状态。

        Returns:
            ``True`` = 已发起等待且尚未收到通过/拒绝信号。
        """
        key = (run_id, step_id)
        entry = self._entries.get(key)
        if entry is None:
            return False
        return entry.approved is None


class CommitRevalidator:
    """commit 重校验器：审批通过后重校验输入/状态一致性。

    防止审批期间状态漂移导致盲目 commit（如资源版本已变、分配所有权已转移）。
    """

    def __init__(self) -> None:
        self._validators: dict[str, CommitValidator] = {}

    def register_validator(self, step_id: str, validator: CommitValidator) -> None:
        """注册步骤的 commit 重校验函数。

        Args:
            step_id: 步骤标识。
            validator: 校验函数 ``(context) -> bool``。
        """
        self._validators[step_id] = validator

    def revalidate(self, step_id: str, context: dict[str, Any]) -> bool:
        """重校验步骤是否可 commit。

        Args:
            step_id: 步骤标识。
            context: 校验上下文（如当前资源版本、分配状态等）。

        Returns:
            ``True`` = 可 commit（状态一致），``False`` = 状态已变不可 commit。
            未注册校验器的步骤默认返回 ``True``（无约束即放行）。
        """
        validator = self._validators.get(step_id)
        if validator is None:
            return True
        return validator(context)


__all__ = [
    "CommitValidator",
    "ApprovalGate",
    "CommitRevalidator",
]
