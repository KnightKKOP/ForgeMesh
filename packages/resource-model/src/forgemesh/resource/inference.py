"""``InferenceService`` 强类型资源模型。

对齐 ``docs/architecture/development-plan.md``：

* 5.1 核心资源模型（Python / Pydantic）；
* 6.1 ``InferenceService`` YAML 示例；
* ADR-002（声明式资源：``spec`` 为期望状态、``status`` 为实际状态）；
* ADR-010（能力驱动异构资源：加速器以类型 + 显式需求表达）；
* ADR-011（运行时版本固定）。

字段命名与资源 API/JSON 表示保持一致（camelCase），便于网关 / CLI / OpenAPI 直接互通。
兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from forgemesh.core.objects import Condition, Resource
from pydantic import BaseModel, ConfigDict, Field


class InferencePhase(StrEnum):
    """``InferenceService`` 生命周期阶段（11 态）。

    取值与 ``states.INFERENCE_TRANSITIONS`` 的键一致；``str`` 混入便于与
    JSON/YAML 中的字符串直接比较与序列化。
    """

    PENDING = "Pending"
    SCHEDULING = "Scheduling"
    PREPARING = "Preparing"
    DEPLOYING = "Deploying"
    VERIFYING = "Verifying"
    READY = "Ready"
    DEGRADED = "Degraded"
    UPDATING = "Updating"
    TERMINATING = "Terminating"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class ModelRef(BaseModel):
    """模型引用（仅引用，不内嵌制品）。

    Attributes:
        reference: 模型引用标识，如 ``models/llama``。
        digest: 制品摘要，用于供应链校验（供应链安全）。
    """

    model_config = ConfigDict(extra="forbid")

    reference: str
    digest: str | None = None


class RuntimeRef(BaseModel):
    """推理运行时引用（版本固定，ADR-011）。

    Attributes:
        provider: 运行时提供方标识，如 ``vllm``。
        version: 固定版本，如 ``0.6.3``；为空表示由平台选择默认版本。
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    version: str | None = None


class AcceleratorSpec(BaseModel):
    """加速器申请（能力驱动，ADR-010）。

    Attributes:
        type: 加速器类型，如 ``nvidia-gpu``。
        count: 申请数量，必须 ``>= 1``。
        minMemoryGiB: 单卡最低显存（GiB），必须 ``>= 1``。
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    count: int = Field(default=1, ge=1)
    minMemoryGiB: int = Field(ge=1)


class ResourcesSpec(BaseModel):
    """资源申请包裹层（对齐 Schema #/$defs/ResourcesSpec）。

    Attributes:
        accelerator: 加速器申请（能力驱动，ADR-010）。
    """

    model_config = ConfigDict(extra="forbid")

    accelerator: AcceleratorSpec


class Availability(BaseModel):
    """可用性期望。

    Attributes:
        replicas: 副本数，必须 ``>= 0``（``0`` 表示显式缩容到零）。
    """

    model_config = ConfigDict(extra="forbid")

    replicas: int = Field(default=1, ge=0)


class InferenceServiceSpec(BaseModel):
    """``InferenceService`` 期望状态（Desired State，ADR-002）。

    Attributes:
        model: 模型引用。
        runtime: 推理运行时引用（版本固定，ADR-011）。
        resources: 资源申请包裹层（含加速器等），对齐 JSON Schema。
        availability: 可用性期望（副本数等）。
    """

    model_config = ConfigDict(extra="forbid")

    model: ModelRef
    runtime: RuntimeRef
    resources: ResourcesSpec
    availability: Availability = Field(default_factory=Availability)


class InferenceServiceStatus(BaseModel):
    """``InferenceService`` 实际状态（Observed State，ADR-002）。

    Attributes:
        phase: 当前生命周期阶段，默认 ``Pending``。
        observedGeneration: 控制器已处理到的 ``metadata.generation``。
        conditions: 结构化状态条件集合。
    """

    model_config = ConfigDict(extra="forbid")

    phase: InferencePhase = InferencePhase.PENDING
    observedGeneration: int = 0
    conditions: list[Condition] = Field(default_factory=list)


class InferenceService(Resource):
    """声明式推理服务资源（``kind = "InferenceService"``）。

    继承 :class:`forgemesh.core.objects.Resource`（``apiVersion`` / ``kind`` /
    ``metadata``），并扩展强类型 ``spec`` / ``status``。
    """

    kind: Literal["InferenceService"] = "InferenceService"
    spec: InferenceServiceSpec
    status: InferenceServiceStatus = Field(default_factory=InferenceServiceStatus)


__all__ = [
    "InferencePhase",
    "ModelRef",
    "RuntimeRef",
    "AcceleratorSpec",
    "ResourcesSpec",
    "Availability",
    "InferenceServiceSpec",
    "InferenceServiceStatus",
    "InferenceService",
]
