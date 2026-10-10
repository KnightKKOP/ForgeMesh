"""ForgeMesh Node Agent 骨架（阶段 0，占位）。

Node Agent 是节点侧**受控**执行组件（architecture 10.1）。本模块仅约定
类型化任务契约与重连/幂等语义，不执行任何真实设备操作。

安全与语义约束（ADR-009 / architecture 10.3）
-------------------------------------------
* **只接受有类型任务**：不存在 ``RUN_SHELL`` / ``EXEC`` 等任意命令类型，
  杜绝远程任意命令执行（与 ``api/protobuf/agent.proto`` 的 ``TaskType`` 一致）。
* **幂等键**：每个 :class:`Task` 携带 ``idempotency_key``。断线重连后，Agent
  与平台以该键去重；控制平面不得仅因未收到结果就认定原操作失败，须先在
  ``execution_id`` 维度查询真实状态再决定是否重试。
* **超时**：任务按 ``deadline`` 截止，超期上报 ``TIMED_OUT``。
* **最小权限**：``requested_permissions`` 声明本次执行所需权限，越权即拒绝。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, StrEnum


class TaskType(StrEnum):
    """Agent 支持的任务类型（受控枚举，无任意命令）。"""

    PROBE_GPU = "PROBE_GPU"
    VALIDATE_DRIVER = "VALIDATE_DRIVER"
    PREPARE_RUNTIME = "PREPARE_RUNTIME"
    DEPLOY_INFERENCE_SERVICE = "DEPLOY_INFERENCE_SERVICE"
    INSPECT_SERVICE = "INSPECT_SERVICE"
    RELEASE_ALLOCATION = "RELEASE_ALLOCATION"


class TaskState(StrEnum):
    """任务执行结果状态（对齐 ``TaskResult.State``）。"""

    UNSPECIFIED = "STATE_UNSPECIFIED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Task:
    """类型化任务（与 ``api/protobuf/agent.proto`` 的 ``Task`` 对齐）。

    Attributes:
        task_id: 全局任务标识。
        execution_id: 一次执行尝试标识（重连恢复以它为单位）。
        idempotency_key: 幂等键，用于重复投递去重。
        type: 任务类型（:class:`TaskType`）。
        deadline: RFC 3339 时间字符串；``None`` 表示无超时。
        requested_permissions: 本次执行请求的最小权限集合。
        input_schema_version: 输入载荷 Schema 版本（ADR-012）。
        correlation_id: 全链路关联 ID（INV-4）。
        input: 类型化输入载荷（bytes，按 input_schema 校验）。
    """

    task_id: str
    execution_id: str
    idempotency_key: str
    type: TaskType
    deadline: str | None = None
    requested_permissions: list[str] = field(default_factory=list)
    input_schema_version: str = "v1"
    correlation_id: str | None = None
    input: bytes = b""


@dataclass(frozen=True)
class TaskResult:
    """任务结果（与 ``TaskResult`` 对齐）。"""

    task_id: str
    execution_id: str
    state: TaskState = TaskState.UNSPECIFIED
    output: bytes = b""
    output_schema_version: str = "v1"
    error_code: str | None = None
    error_message: str | None = None
    completed_at: str | None = None


class NodeAgent(ABC):
    """Node Agent 抽象接口骨架（占位）。

    与 ``api/protobuf/agent.proto`` 的 ``service NodeAgent`` 一一对应。
    """

    @abstractmethod
    async def register(self, *, agent_id: str, agent_version: str,
                       capabilities: list[str]) -> bool:
        """注册并协商能力/兼容性。返回是否被接受。"""
        raise NotImplementedError

    @abstractmethod
    async def heartbeat(self, *, running_execution_ids: list[str]) -> None:
        """上报心跳及在途执行，用于离线检测与重连恢复。"""
        raise NotImplementedError

    @abstractmethod
    async def poll_task(self, *, agent_id: str) -> Task | None:
        """拉取一个待执行类型化任务；无任务时返回 ``None``。"""
        raise NotImplementedError

    @abstractmethod
    async def report_result(self, result: TaskResult) -> bool:
        """上报任务结果（按 ``idempotency_key`` 去重）。"""
        raise NotImplementedError

    @abstractmethod
    async def cancel_task(self, *, task_id: str, execution_id: str,
                          reason: str = "") -> TaskResult:
        """取消任务并返回其结果。"""
        raise NotImplementedError


__all__ = [
    "TaskType",
    "TaskState",
    "Task",
    "TaskResult",
    "NodeAgent",
]
