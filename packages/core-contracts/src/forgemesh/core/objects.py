"""ForgeMesh 核心资源模型（声明式资源基类）。

对齐 ``docs/architecture/development-plan.md`` 5.1 与 architecture 5.2：

* ``apiVersion`` 采用组/版本形式 ``forgemesh.io/v1alpha1``（:data:`GROUP_VERSION`）。
* ``ObjectMeta`` 承载公共元数据；字段名与资源 API/JSON 表示保持一致（camelCase），
  便于序列化后直接与 ``kubectl`` 风格工具/网关互通。
* ``Condition`` 为结构化状态条件（True/False/Unknown 三值）。
* ``Resource`` 是所有声明式资源的基类：Desired(spec) / Observed(status) 的具体字段
  由子类（如 ``forgemesh.resource.*``）通过 ``ResourceBase`` 扩展；阶段 0 仅约定
  ``apiVersion`` / ``kind`` / ``metadata`` 与条件集合。

**版本语义**：``metadata.generation`` 表示期望状态（spec）版本，spec 变更时递增；
``metadata.resourceVersion`` 为乐观并发 CAS 依据，每次成功写入递增。本模块的
:meth:`Resource.with_generation_bump` 提供不可变的 generation 递增（返回副本）。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: 资源组/版本常量，作为所有资源 ``apiVersion`` 的默认值。
GROUP_VERSION = "forgemesh.io/v1alpha1"

#: 兼容别名（部分文档/上游以 ``API_VERSION`` 命名）。
API_VERSION = GROUP_VERSION


class ObjectMeta(BaseModel):
    """所有资源对象的公共元数据（对齐 architecture 5.2）。

    Attributes:
        name: 资源名（同一 kind + namespace 内唯一）。
        namespace: 租户内的逻辑命名空间，可为空（集群级资源）。
        uid: 稳定标识，资源生命周期内不变（INV-4 可追溯）。
        labels: 用于选择器过滤的键值对。
        annotations: 非标识性的附加元数据（不参与选择）。
        resourceVersion: 乐观并发版本（CAS 依据，字符串以便与 API 表示一致）。
        generation: 期望状态版本，spec 变更时递增。
        tenant_id: 租户标识（多租户边界，alias ``tenantId``）。
        observed_generation: 控制器已处理到的 ``generation``（alias ``observedGeneration``）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    namespace: str | None = None
    uid: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    resourceVersion: str | None = None
    generation: int = 0
    tenant_id: str | None = Field(default=None, alias="tenantId")
    observed_generation: int = Field(default=0, alias="observedGeneration")


class Condition(BaseModel):
    """结构化状态条件（对齐 architecture 5.2）。

    Attributes:
        type: 条件类型（如 ``Ready`` / ``Scheduled`` / ``Degraded``）。
        status: 三值状态 ``True`` / ``False`` / ``Unknown``。
        reason: 机器可读的简短原因（CamelCase）。
        message: 人类可读的详细说明。
        lastTransitionTime: 状态最近一次变化的时间。
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    status: Literal["True", "False", "Unknown"]
    reason: str = ""
    message: str = ""
    lastTransitionTime: datetime | None = None


class Resource(BaseModel):
    """声明式资源基类。

    阶段 0 的最小充分契约：``apiVersion`` / ``kind`` / ``metadata``，外加一个
    基类级 :attr:`conditions` 集合供控制器读写状态条件。强类型资源（如
    ``InferenceService``）在此基础上扩展 spec/status。

    Attributes:
        apiVersion: 资源组/版本，默认 :data:`GROUP_VERSION`。
        kind: 资源类型标识（如 ``InferenceService``）。
        metadata: 公共元数据。
        conditions: 资源条件集合（同一 ``type`` 至多一条）。
    """

    model_config = ConfigDict(extra="forbid")

    apiVersion: str = GROUP_VERSION
    kind: str
    metadata: ObjectMeta
    conditions: list[Condition] = Field(default_factory=list)

    def with_generation_bump(self) -> Resource:
        """返回一份深拷贝，并将其 ``metadata.generation`` 递增 1。

        保持不可变语义：不修改当前对象，便于在协调循环中安全地派生新期望版本。
        """
        clone = self.model_copy(deep=True)
        clone.metadata.generation += 1
        return clone

    def set_condition(self, cond: Condition) -> Resource:
        """写入/覆盖一条条件。

        同一 ``type`` 已存在时**就地覆盖**（保持原列表位置），否则追加。
        返回 ``self`` 以支持链式调用。
        """
        for index, existing in enumerate(self.conditions):
            if existing.type == cond.type:
                self.conditions[index] = cond
                break
        else:
            self.conditions.append(cond)
        return self

    def get_condition(self, type: str) -> Condition | None:
        """按 ``type`` 返回条件；不存在时返回 ``None``。"""
        for existing in self.conditions:
            if existing.type == type:
                return existing
        return None


__all__ = [
    "GROUP_VERSION",
    "API_VERSION",
    "ObjectMeta",
    "Condition",
    "Resource",
]
