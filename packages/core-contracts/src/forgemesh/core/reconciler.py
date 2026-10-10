"""ForgeMesh 协调（Reconciliation）契约（9 步闭环）。

对齐 ``docs/architecture/development-plan.md`` 5.3。协调器负责将资源的
**期望状态（spec）** 收敛到 **实际状态（observed）**，其核心约束为：

**幂等性（必须，INV-1/INV-3）**：同一 ``dedup_key`` 的协调**不得重复产生副作用**。
实现方应：

1. 以 ``dedup_key``（由 kind/namespace/name/uid/generation/operation 派生）
   作为去重键，在开始执行有副作用的操作前检查是否已处理完成；
2. 若该目标 generation 已处理且无差异，直接返回 ``done=True``（不再触发下游）；
3. 有副作用的操作（分配、工作流启动等）须携带该去重键，由下游保证至多一次。

协调器**不得**绕过 Policy/Scheduler 直接写资源终态；写状态仅经 Registry。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .objects import Resource


class ReconcileContext(BaseModel):
    """一次协调调用的上下文。

    Attributes:
        resource: 被协调的目标资源（含期望状态与元数据）。
        observed: 实际状态快照（设备/分配/工作流/服务等的观测结果）。
        correlation_id: 全链路追踪 ID（贯穿审计与事件）。
        tenant_id: 多租户边界标识。
        dedup_key: **幂等去重键**；同一键不得重复副作用。
    """

    model_config = ConfigDict(extra="forbid")

    resource: Resource
    observed: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str
    tenant_id: str
    dedup_key: str


class ReconcileResult(BaseModel):
    """协调结果。

    Attributes:
        requeue_after: 若需重新协调，建议的延迟秒数（``None`` 表示不再安排）。
        status_patch: 仅允许更新 status 子树的补丁（不得改 spec）。
        done: 本轮协调是否已达期望终态。
    """

    model_config = ConfigDict(extra="forbid")

    requeue_after: float | None = None
    status_patch: dict[str, Any] = Field(default_factory=dict)
    done: bool = False


class Reconciler(ABC):
    """协调器抽象基类。

    实现方的 :meth:`reconcile` 必须**幂等**：对同一 ``dedup_key`` 重复调用不得
    产生重复副作用（例如重复分配设备、重复启动工作流）。
    """

    @abstractmethod
    async def reconcile(self, ctx: ReconcileContext) -> ReconcileResult:
        """执行一轮协调，返回下一步动作。

        必须保证：同一 ``ctx.dedup_key`` 的副作用至多发生一次。
        """
        raise NotImplementedError


__all__ = ["ReconcileContext", "ReconcileResult", "Reconciler"]
