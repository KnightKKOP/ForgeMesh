"""租户 / 主体 / 配额策略模型。

对齐 ``docs/architecture/development-plan.md``：

* 5.1 核心资源模型；
* 6.6 ``Tenant`` / ``QuotaPolicy`` YAML 示例；
* 7.2 DDL 草案（``tenant`` / ``principal`` 表）；
* 多租户隔离（``tenant_id`` 贯穿身份、资源、事件与计量边界）。

字段命名与 DDL 保持一致（snake_case），便于持久化层直接映射。
兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: 租户有效状态取值（对齐 DDL：active | suspended | terminating）。
DEFAULT_TENANT_STATUS = "active"


class Tenant(BaseModel):
    """租户（隔离边界）。

    Attributes:
        id: 租户 id（主键）。
        status: 租户状态，默认 ``active``。
        quota_policy_id: 关联的 :class:`QuotaPolicy` id；为空表示无显式配额。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: str = DEFAULT_TENANT_STATUS
    quota_policy_id: str | None = None


class Principal(BaseModel):
    """主体（用户 / 服务 / Agent 身份，归属某一租户）。

    Attributes:
        id: 主体 id（主键）。
        tenant_id: 归属租户 id。
        identity_type: 身份类型（``user`` | ``service`` | ``agent``）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    identity_type: str


class QuotaPolicy(BaseModel):
    """配额策略（硬/软限制与突发上限）。

    Attributes:
        limits: 额度上限（如 ``{"accelerators": {"nvidia-gpu": {"max": 8}}}``）。
        burst: 突发上限（如 ``{"tokensPerMinute": 250000}``）。
        enforcement: 执行方式（``hard`` | ``soft``），默认 ``hard``。
    """

    model_config = ConfigDict(extra="forbid")

    limits: dict[str, Any] = Field(default_factory=dict)
    burst: dict[str, Any] = Field(default_factory=dict)
    enforcement: str = "hard"


__all__ = [
    "DEFAULT_TENANT_STATUS",
    "Tenant",
    "Principal",
    "QuotaPolicy",
]
