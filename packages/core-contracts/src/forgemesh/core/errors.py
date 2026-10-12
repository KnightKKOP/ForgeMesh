"""ForgeMesh 统一异常体系。

阶段 0 核心契约：所有子包共享同一异常基类 :class:`ForgeMeshError`，
以便调用方在编排层做统一错误归类与映射（HTTP/gRPC 状态码、重试判定等）。

设计约束（对齐 development-plan 5.2 并发/幂等语义表）：

* ``NotFoundError``    —— 资源不存在；**跨租户访问亦视为 NotFound**，不泄露存在性。
* ``ConflictError``    —— 乐观并发写失败（resourceVersion 不匹配）或唯一约束冲突。
* ``ForbiddenError``   —— 鉴权/准入被拒绝（Policy deny）。
* ``PreconditionFailedError`` —— 前置条件不满足（如需要审批、版本漂移后 spec 已变）。
* ``InvalidStateTransition``  —— 状态机非法转移（对齐 workflow/resource 状态机）。
"""

from __future__ import annotations


class ForgeMeshError(Exception):
    """ForgeMesh 领域异常基类。

    Attributes:
        message: 人类可读的错误描述。
        details: 可选的附加上下文（用于审计/诊断），默认 ``None``。
    """

    def __init__(self, message: str = "", details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict | None = details

    def __str__(self) -> str:  # pragma: no cover - 简单委托
        if self.details:
            return f"{self.message} (details={self.details!r})"
        return self.message


class NotFoundError(ForgeMeshError):
    """目标资源不存在，或调用方无权感知其存在（跨租户隐藏）。"""


class ConflictError(ForgeMeshError):
    """乐观并发冲突：提交的 resourceVersion 与当前版本不一致，或资源已存在。"""


class ForbiddenError(ForgeMeshError):
    """操作被鉴权/策略拒绝（Policy 返回 deny）。"""


class PreconditionFailedError(ForgeMeshError):
    """前置条件不满足（如缺少审批、目标 generation 已过期）。"""


class InvalidStateTransition(ForgeMeshError):
    """非法状态转移：源状态到目标状态不在允许的转移集合内。"""


class GovernanceBypassError(ForgeMeshError):
    """治理绕过：跳过 Policy 鉴权直接分配资源，违反 INV-3。"""


__all__ = [
    "ForgeMeshError",
    "NotFoundError",
    "ConflictError",
    "ForbiddenError",
    "PreconditionFailedError",
    "InvalidStateTransition",
    "GovernanceBypassError",
]
