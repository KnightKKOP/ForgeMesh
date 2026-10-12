"""ForgeMesh 资源模型（命名空间包 ``forgemesh.resource``）。

阶段 0 强类型资源与领域实体模型，对齐 ``docs/architecture/development-plan.md``
第 5.1 节（核心资源模型）、第 6.1/6.4/6.6 节（关键 YAML 示例）与第 7.2 节（DDL 草案）：

* :mod:`forgemesh.resource.inference`  —— ``InferenceService`` 及其 spec/status 子模型。
* :mod:`forgemesh.resource.node`       —— ``Node`` / ``Accelerator`` / ``DeviceInstance`` / ``DeviceProfile`` 等执行平面实体。
* :mod:`forgemesh.resource.allocation` —— 资源分配与租约（并发安全核心）。
* :mod:`forgemesh.resource.tenant`     —— 租户 / 主体 / 配额策略。
* :mod:`forgemesh.resource.states`     —— 通用状态机与资源生命周期转移表。

本包仅依赖 ``forgemesh-core``（Pydantic v2），不反向依赖任何插件或控制平面实现。
"""

from __future__ import annotations

from .allocation import Allocation, AllocationStatus, ResourceLease
from .inference import (
    AcceleratorSpec,
    Availability,
    Endpoint,
    EndpointProtocol,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    RuntimeRef,
)
from .node import (
    Accelerator,
    DeviceInstance,
    DeviceProfile,
    HealthReport,
    Node,
    RuntimeInstance,
    Topology,
)
from .states import (
    ALLOCATION_STATE_MACHINE,
    ALLOCATION_STATES,
    ALLOCATION_TRANSITIONS,
    INFERENCE_STATE_MACHINE,
    INFERENCE_STATES,
    INFERENCE_TRANSITIONS,
    StateMachine,
)
from .tenant import Principal, QuotaPolicy, Tenant

__all__ = [
    # inference
    "InferencePhase",
    "ModelRef",
    "RuntimeRef",
    "AcceleratorSpec",
    "Availability",
    "InferenceServiceSpec",
    "EndpointProtocol",
    "Endpoint",
    "InferenceServiceStatus",
    "InferenceService",
    # node
    "Node",
    "Accelerator",
    "DeviceInstance",
    "DeviceProfile",
    "Topology",
    "HealthReport",
    "RuntimeInstance",
    # allocation
    "AllocationStatus",
    "Allocation",
    "ResourceLease",
    # tenant
    "Tenant",
    "Principal",
    "QuotaPolicy",
    # states
    "StateMachine",
    "INFERENCE_STATES",
    "INFERENCE_TRANSITIONS",
    "INFERENCE_STATE_MACHINE",
    "ALLOCATION_STATES",
    "ALLOCATION_TRANSITIONS",
    "ALLOCATION_STATE_MACHINE",
]
