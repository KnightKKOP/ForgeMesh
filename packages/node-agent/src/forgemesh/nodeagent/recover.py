"""断线重连与任务恢复（WP-3.1）。

:class:`RecoveryManager` 在 Node Agent 重连后，对本地记录的"在途执行"逐一向
控制平面查询**真实状态**，再据此决策恢复动作——**不盲目重试**（INV-4/INV-5）：

* 远端已 ``succeeded`` → ``report_success``（本地误以为在跑，补报成功）；
* 远端已 ``failed``    → ``report_failure``（补报失败）；
* 远端已 ``cancelled`` → ``cancel``（同步取消）；
* 远端仍 ``running``   → ``resume``（继续等待）；
* 远端未知/不存在     → ``resume``（乐观恢复，等待下次心跳/查询确认）。

:class:`IdempotencyGuard` 以 ``task_id`` + ``idempotency_key`` 去重，防止重连后
重复执行同一幂等任务。

对齐 architecture 10.3（重连/幂等语义）与 ADR-009。兼容 Python 3.9+。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forgemesh.nodeagent.server import AgentServer


@dataclass(frozen=True)
class RecoveryAction:
    """恢复动作。

    Attributes:
        task_id: 关联任务标识。
        action: 恢复策略，取值 ``resume`` / ``cancel`` / ``report_success`` /
            ``report_failure``。
        reason: 决策原因（人类可读）。
    """

    task_id: str
    action: str
    reason: str


@dataclass(frozen=True)
class RunningExecution:
    """本地记录的在途执行。

    Attributes:
        task_id: 任务标识。
        execution_id: 一次执行尝试标识（重连恢复以它为单位查询真实状态）。
    """

    task_id: str
    execution_id: str


class RecoveryManager:
    """断线恢复管理（INV-4/INV-5：先查询真实状态再决策）。"""

    async def recover(
        self,
        server: AgentServer,
        *,
        agent_id: str,
        running_executions: list[RunningExecution],
    ) -> list[RecoveryAction]:
        """查询未完成任务真实状态，返回恢复动作列表。

        Args:
            server: 控制平面（提供 ``get_execution_state``）。
            agent_id: 节点 Agent 标识。
            running_executions: 本地记录的在途执行列表。
        """
        actions: list[RecoveryAction] = []
        for ex in running_executions:
            state = await server.get_execution_state(ex.execution_id)
            action, reason = self._decide(state)
            actions.append(
                RecoveryAction(task_id=ex.task_id, action=action, reason=reason)
            )
        return actions

    @staticmethod
    def _decide(state: str | None) -> tuple[str, str]:
        """根据远端真实状态决策恢复动作与原因。"""
        if state == "succeeded":
            return "report_success", "execution already succeeded remotely"
        if state == "failed":
            return "report_failure", "execution already failed remotely"
        if state == "cancelled":
            return "cancel", "execution cancelled remotely"
        if state == "running":
            return "resume", "execution still running remotely"
        return "resume", "execution state unknown; resuming optimistically"


class IdempotencyGuard:
    """幂等保护（set 缓存已处理任务）。

    以 ``task_id`` + ``idempotency_key`` 组合键去重。``check_and_mark`` 返回
    ``True`` 表示首次处理，``False`` 表示重复（已处理过）。
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def check_and_mark(self, task_id: str, idempotency_key: str) -> bool:
        """检查并标记任务。

        Returns:
            ``True`` 表示首次处理（已标记）；``False`` 表示重复。
        """
        key = f"{task_id}:{idempotency_key}"
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


__all__ = [
    "RecoveryAction",
    "RunningExecution",
    "RecoveryManager",
    "IdempotencyGuard",
]
