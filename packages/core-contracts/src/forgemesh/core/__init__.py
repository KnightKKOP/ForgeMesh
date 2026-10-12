"""ForgeMesh 核心契约（命名空间包 ``forgemesh.core``）。

本包定义阶段 0 的跨子包公共契约：

* :mod:`forgemesh.core.objects`   —— 资源/元数据/条件基类与 ``GROUP_VERSION``。
* :mod:`forgemesh.core.registry`  —— 权威资源注册表（乐观并发）。
* :mod:`forgemesh.core.reconciler`—— 幂等协调契约。
* :mod:`forgemesh.core.scheduler` —— 唯一授权分配点（决策≠授权）。
* :mod:`forgemesh.core.secrets`   —— 密钥引用（仅引用不含明文）。
* :mod:`forgemesh.core.errors`    —— 统一异常体系。
"""

from __future__ import annotations

from .errors import (
    ConflictError,
    ForbiddenError,
    ForgeMeshError,
    InvalidStateTransition,
    NotFoundError,
    PreconditionFailedError,
)
from .objects import (
    API_VERSION,
    GROUP_VERSION,
    Condition,
    ObjectMeta,
    Resource,
)
from .reconciler import ReconcileContext, Reconciler, ReconcileResult
from .registry import ResourceRegistry
from .scheduler import (
    AllocatedDevice,
    AllocationRef,
    FeasibilityResult,
    ResourceRef,
    Scheduler,
    ScheduleRequest,
)
from .secrets import SecretRef

__all__ = [
    # objects
    "GROUP_VERSION",
    "API_VERSION",
    "ObjectMeta",
    "Condition",
    "Resource",
    # registry
    "ResourceRegistry",
    # reconciler
    "ReconcileContext",
    "ReconcileResult",
    "Reconciler",
    # scheduler
    "ResourceRef",
    "ScheduleRequest",
    "FeasibilityResult",
    "AllocatedDevice",
    "AllocationRef",
    "Scheduler",
    # secrets
    "SecretRef",
    # errors
    "ForgeMeshError",
    "NotFoundError",
    "ConflictError",
    "ForbiddenError",
    "PreconditionFailedError",
    "InvalidStateTransition",
]
