"""QoS 基础策略插件 Provider（对齐 development-plan WP-4.3）。

基于已实现的 :mod:`forgemesh.policy.qos` 模块，封装为可插拔的 QoS Provider，
供插件运行时通过统一接口调用 QoS 准入判断。

设计约束：

* **薄封装**：Provider 仅做请求解析与结果转换，核心逻辑复用
  :class:`forgemesh.policy.qos.QoSManager`，避免逻辑重复。
* **宽松输入**：``evaluate`` 接受 ``dict[str, Any]``，从中解析 ``tenant_id``
  与 ``priority``；缺失字段使用合理默认，降低调用方耦合。
* **优先级可移植**：:class:`QoSProviderResult.priority` 以字符串形式返回
  优先级名称（如 ``"NORMAL"``），避免跨进程传递枚举序列化问题。

兼容 Python 3.9+。
"""

from __future__ import annotations

import uuid
from typing import Any

from forgemesh.policy.qos import Priority, QoSConfig, QoSDecision, QoSManager, QoSRequest
from pydantic import BaseModel, ConfigDict


class QoSProviderResult(BaseModel):
    """QoS Provider 评估结果。

    Attributes:
        allowed: 是否准入。
        reason: 决策理由——``"admitted"`` / ``"rate_limited"`` /
            ``"max_concurrent_reached"``。
        priority: 请求优先级名称（如 ``"NORMAL"``）。
        queue_position: 队列位置；准入时为 0（立即处理），未入队时为 ``None``。
    """

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str
    priority: str
    queue_position: int | None = None


def _parse_priority(value: Any) -> Priority:
    """将任意输入解析为 :class:`Priority` 枚举。

    接受 :class:`Priority` 实例、枚举名称（``"HIGH"``）或枚举值（``int``）。
    无法识别时回退到 :attr:`Priority.NORMAL`。

    Args:
        value: 待解析的优先级值。

    Returns:
        对应的 :class:`Priority` 枚举成员。
    """
    if isinstance(value, Priority):
        return value
    if isinstance(value, str):
        try:
            return Priority[value.upper()]
        except KeyError:
            return Priority.NORMAL
    if isinstance(value, int):
        try:
            return Priority(value)
        except ValueError:
            return Priority.NORMAL
    return Priority.NORMAL


class QoSProvider:
    """QoS 策略 Provider：封装 :class:`QoSManager` 为可插拔组件。

    Args:
        config: QoS 配置，用于初始化内部 :class:`QoSManager`。
    """

    def __init__(self, config: QoSConfig) -> None:
        self._manager = QoSManager(config)

    @property
    def manager(self) -> QoSManager:
        """暴露内部 :class:`QoSManager`，供需要释放并发槽的调用方使用。"""
        return self._manager

    async def evaluate(self, request: dict[str, Any]) -> QoSProviderResult:
        """评估请求 QoS，判断是否准入。

        从 ``request`` 中解析以下字段：

        * ``request_id``：请求唯一标识，缺失时自动生成 UUID。
        * ``tenant_id``：租户标识，**必填**，缺失抛 :class:`KeyError`。
        * ``priority``：优先级，可选，缺失或无法识别时默认 :attr:`Priority.NORMAL`。
        * ``payload``：请求负载，可选，默认空字典。

        Args:
            request: 原始请求字典。

        Returns:
            :class:`QoSProviderResult`。

        Raises:
            KeyError: ``request`` 缺少 ``tenant_id``。
        """
        tenant_id = request["tenant_id"]
        request_id = request.get("request_id", uuid.uuid4().hex)
        priority = _parse_priority(request.get("priority"))
        payload = request.get("payload", {})

        qos_request = QoSRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            priority=priority,
            payload=payload,
        )
        decision: QoSDecision = await self._manager.admit(qos_request)
        return _to_provider_result(decision)


def _to_provider_result(decision: QoSDecision) -> QoSProviderResult:
    """将 :class:`QoSDecision` 转换为 :class:`QoSProviderResult`。"""
    return QoSProviderResult(
        allowed=decision.allowed,
        reason=decision.reason,
        priority=decision.priority.name,
        queue_position=decision.queue_position,
    )


__all__ = [
    "QoSProvider",
    "QoSProviderResult",
]
