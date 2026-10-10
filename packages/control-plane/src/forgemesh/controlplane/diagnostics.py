"""统一诊断包：聚合资源/工作流/插件/决策/日志，按租户权限受限输出。

对齐 development-plan WP-4.5。提供以下能力：

* :class:`DiagnosticBundle` —— 诊断包（pydantic 模型），聚合各维度快照。
* :class:`DiagnosticCollector` —— 诊断包采集器，按租户权限受限采集，
  日志自动脱敏，correlation_id 累计贯穿。

设计约束：

* **按租户权限受限**：collect 只采集指定租户的数据，绝不泄露其他租户信息。
  插件状态为全局配置，不按租户隔离。
* **日志脱敏**：敏感字段（password/token/secret/api_key/credentials）
  递归替换为 ``"***"``。
* **correlation_id 累计贯穿**：collect 优先复用 :class:`CorrelationContext`
  中的 correlation_id，否则生成新 ID，写入 bundle 并贯穿全链路。

兼容 Python 3.9+（运行环境 3.12）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from forgemesh.controlplane.observability import CorrelationContext, new_correlation_id
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------- #
# 敏感字段脱敏
# ---------------------------------------------------------------------- #

#: 敏感字段名集合（小写匹配）。
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {"password", "token", "secret", "api_key", "credentials"}
)

#: 脱敏占位符。
_REDACTED = "***"


def _redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏敏感字段。

    将键名（小写匹配）属于 :data:`SENSITIVE_FIELDS` 的字段值替换为
    ``"***"``，递归处理嵌套字典。非字典值原样返回。不修改原字典。

    Args:
        data: 待脱敏的字典。

    Returns:
        脱敏后的新字典。
    """
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in SENSITIVE_FIELDS:
            redacted[key] = _REDACTED
        elif isinstance(value, dict):
            redacted[key] = _redact_sensitive(value)
        else:
            redacted[key] = value
    return redacted


# ---------------------------------------------------------------------- #
# 诊断包
# ---------------------------------------------------------------------- #


class DiagnosticBundle(BaseModel):
    """诊断包：聚合资源/工作流/插件/决策/日志/指标的统一快照。

    Attributes:
        bundle_id: 诊断包唯一标识。
        tenant_id: 租户标识。
        generated_at: 生成时间戳（epoch 秒）。
        resources: 资源快照列表。
        workflows: 工作流状态列表。
        plugins: 插件状态列表（全局，不按租户隔离）。
        decisions: 决策记录列表。
        logs: 相关日志列表（已脱敏）。
        metrics: 指标快照。
        correlation_id: 关联 ID，全链路贯穿。
    """

    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    tenant_id: str
    generated_at: float
    resources: list[dict[str, Any]] = Field(default_factory=list)
    workflows: list[dict[str, Any]] = Field(default_factory=list)
    plugins: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    logs: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str


# ---------------------------------------------------------------------- #
# 诊断包采集器
# ---------------------------------------------------------------------- #


class DiagnosticCollector:
    """诊断包采集器：聚合各维度信息，按租户权限受限输出。

    内部维护各维度数据存储，按 ``tenant_id`` 索引（插件除外，全局共享）。
    采集时只返回指定租户的数据，绝不泄露其他租户信息。

    用法::

        collector = DiagnosticCollector()
        collector.register_resource("t1", {"id": "r1", "type": "gpu"})
        bundle = await collector.collect("t1")
    """

    def __init__(self) -> None:
        self._resources: dict[str, list[dict[str, Any]]] = {}
        self._workflows: dict[str, list[dict[str, Any]]] = {}
        self._plugins: list[dict[str, Any]] = []
        self._decisions: dict[str, list[dict[str, Any]]] = {}
        self._logs: dict[str, list[dict[str, Any]]] = {}
        self._metrics: dict[str, Any] = {}

    # -- 数据注册（供外部模块注入）-------------------------------------------

    def register_resource(self, tenant_id: str, resource: dict[str, Any]) -> None:
        """注册资源快照（按租户隔离）。"""
        self._resources.setdefault(tenant_id, []).append(dict(resource))

    def register_workflow(self, tenant_id: str, workflow: dict[str, Any]) -> None:
        """注册工作流状态（按租户隔离）。"""
        self._workflows.setdefault(tenant_id, []).append(dict(workflow))

    def register_plugin(self, plugin: dict[str, Any]) -> None:
        """注册插件状态（全局，不按租户隔离）。"""
        self._plugins.append(dict(plugin))

    def register_decision(self, tenant_id: str, decision: dict[str, Any]) -> None:
        """注册决策记录（按租户隔离）。"""
        self._decisions.setdefault(tenant_id, []).append(dict(decision))

    def register_log(self, tenant_id: str, log: dict[str, Any]) -> None:
        """注册日志（按租户隔离，采集时脱敏）。"""
        self._logs.setdefault(tenant_id, []).append(dict(log))

    def set_metrics(self, metrics: dict[str, Any]) -> None:
        """设置指标快照。"""
        self._metrics = dict(metrics)

    # -- 采集（按租户权限受限）-----------------------------------------------

    async def collect_resources(self, tenant_id: str) -> list[dict[str, Any]]:
        """采集资源快照（只返回指定租户的数据）。"""
        return [dict(r) for r in self._resources.get(tenant_id, [])]

    async def collect_workflows(self, tenant_id: str) -> list[dict[str, Any]]:
        """采集工作流状态（只返回指定租户的数据）。"""
        return [dict(w) for w in self._workflows.get(tenant_id, [])]

    async def collect_plugins(self) -> list[dict[str, Any]]:
        """采集插件状态（全局，不按租户隔离）。"""
        return [dict(p) for p in self._plugins]

    async def collect_decisions(self, tenant_id: str) -> list[dict[str, Any]]:
        """采集决策记录（只返回指定租户的数据）。"""
        return [dict(d) for d in self._decisions.get(tenant_id, [])]

    async def collect_logs(
        self,
        tenant_id: str,
        time_range: tuple[float, float] | None,
    ) -> list[dict[str, Any]]:
        """采集日志（只返回指定租户的数据，脱敏后输出）。

        Args:
            tenant_id: 租户标识。
            time_range: 可选时间范围 ``(start, end)``（epoch 秒），
                按日志 ``timestamp`` 字段过滤；为 ``None`` 时不过滤。

        Returns:
            脱敏后的日志列表。
        """
        logs = self._logs.get(tenant_id, [])
        result: list[dict[str, Any]] = []
        for entry in logs:
            if time_range is not None:
                ts = entry.get("timestamp")
                if not isinstance(ts, int | float):
                    continue
                if ts < time_range[0] or ts > time_range[1]:
                    continue
            result.append(_redact_sensitive(entry))
        return result

    def collect_metrics(self) -> dict[str, Any]:
        """采集指标快照。"""
        return dict(self._metrics)

    # -- 聚合采集 ------------------------------------------------------------

    async def collect(
        self,
        tenant_id: str,
        time_range: tuple[float, float] | None = None,
    ) -> DiagnosticBundle:
        """采集诊断包：聚合各维度信息，按租户权限受限输出。

        优先复用 :class:`CorrelationContext` 中的 correlation_id，
        否则生成新 ID，写入 bundle 并贯穿全链路。

        Args:
            tenant_id: 租户标识。
            time_range: 可选时间范围，仅作用于日志采集。

        Returns:
            :class:`DiagnosticBundle`。
        """
        # correlation_id 累计贯穿：优先复用上下文中的 ID
        correlation_id = CorrelationContext.get_correlation_id()
        if correlation_id is None:
            correlation_id = new_correlation_id()

        resources = await self.collect_resources(tenant_id)
        workflows = await self.collect_workflows(tenant_id)
        plugins = await self.collect_plugins()
        decisions = await self.collect_decisions(tenant_id)
        logs = await self.collect_logs(tenant_id, time_range)
        metrics = self.collect_metrics()

        return DiagnosticBundle(
            bundle_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            generated_at=time.time(),
            resources=resources,
            workflows=workflows,
            plugins=plugins,
            decisions=decisions,
            logs=logs,
            metrics=metrics,
            correlation_id=correlation_id,
        )


__all__ = [
    "SENSITIVE_FIELDS",
    "DiagnosticBundle",
    "DiagnosticCollector",
]
