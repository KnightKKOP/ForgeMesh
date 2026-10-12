"""通用状态机与资源生命周期转移表。

对齐 ``docs/architecture/development-plan.md``：

* 5.1 核心资源模型（``InferenceService`` 阶段）；
* 7.2 DDL 草案（``allocation.status``：``Active | Released | Expired``）；
* ADR-002（声明式资源：协调控制器驱动状态迁移）。

提供 :class:`StateMachine` 用于对「当前状态 → 目标状态」的转移做**统一校验**：
非法转移抛出 :class:`forgemesh.core.errors.InvalidStateTransition`（状态变更必须
可校验、可追溯）。本模块同时导出推理服务与资源分配两张权威转移表。

**推理服务转移语义**（``INFERENCE_TRANSITIONS``）::

    Pending → Scheduling → Preparing → Deploying → Verifying → Ready
    Ready ↔ Degraded / Updating
    任意非终态（含 Failed / Unknown）→ Terminating（删除入口，受控释放）
    任意非终态 → Failed / Unknown
    Terminating → ∅（唯一终态，无出边；finalizer 清空后由注册表物理删除）

其中 ``Terminating`` 是**唯一终态**：进入后无任何出边，由协调器驱动
「受控释放 → 清空 finalizer → 物理删除」闭环（对齐 development-plan 第 7 章）。
``Failed`` / ``Unknown`` 不再是吸收态——允许 ``Failed / Unknown → Terminating``
以便人工清理失败资源（删除入口必须对所有现实状态可达）。

**分配转移语义**（``ALLOCATION_TRANSITIONS``）::

    Active → {Released, Expired}
    Released / Expired → ∅（终态）
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from forgemesh.core.errors import InvalidStateTransition

from .allocation import AllocationStatus
from .inference import InferencePhase


class StateMachine:
    """通用有限状态机：对状态转移做白名单校验。

    采用「显式白名单」而非「默认允许」：未在 ``transitions`` 中声明的
    ``(current, target)`` 一律视为非法，避免漏配产生越权的状态迁移。

    Attributes:
        name: 状态机名称（用于诊断信息）。
        states: 合法状态集合（用于校验源状态是否被认知）。
        transitions: 转移表，``{源状态: {允许的目标状态, ...}}``。
    """

    def __init__(
        self,
        states: Iterable[str],
        transitions: Mapping[str, Iterable[str]],
        name: str = "state-machine",
    ) -> None:
        """构造状态机。

        Args:
            states: 合法状态的可迭代集合。
            transitions: 转移表；每个值会被复制为 ``set``，构造方后续修改不影响本实例。
            name: 状态机名称，默认 ``"state-machine"``。
        """
        self.name = name
        self.states: set[str] = set(states)
        self.transitions: dict[str, set[str]] = {
            source: set(targets) for source, targets in transitions.items()
        }

    def can(self, current: str, target: str) -> bool:
        """判断 ``current → target`` 是否为合法转移。

        源状态不在 :attr:`states` 中时返回 ``False``（未知状态不可作为起点）。
        """
        if current not in self.states:
            return False
        return target in self.transitions.get(current, set())

    def validate(self, current: str, target: str) -> None:
        """校验 ``current → target``；非法时抛出 :class:`InvalidStateTransition`。

        Args:
            current: 源状态。
            target: 目标状态。

        Raises:
            InvalidStateTransition: 当该转移不在白名单内（含未知源状态）。
        """
        if self.can(current, target):
            return
        allowed = sorted(self.transitions.get(current, set()))
        raise InvalidStateTransition(
            f"非法状态转移：{self.name} 不允许 {current!r} -> {target!r}",
            details={
                "machine": self.name,
                "current": current,
                "target": target,
                "allowed": allowed,
            },
        )

    def __repr__(self) -> str:  # pragma: no cover - 简单表示
        return f"StateMachine(name={self.name!r}, states={len(self.states)})"


# ---------------------------------------------------------------------------
# 推理服务状态机
# ---------------------------------------------------------------------------

#: ``InferenceService`` 的全部合法状态集合。
INFERENCE_STATES: set[str] = {phase.value for phase in InferencePhase}

#: ``InferenceService`` 权威转移表。键为源状态，值为允许的目标状态集合。
#: ``Terminating`` 为唯一终态（无出边，finalizer 清空后物理删除）；其余所有
#: 状态（含 ``Failed`` / ``Unknown``，供人工清理）均可转移到 ``Terminating``。
INFERENCE_TRANSITIONS: dict[str, set[str]] = {
    InferencePhase.PENDING.value: {
        InferencePhase.SCHEDULING.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.SCHEDULING.value: {
        InferencePhase.PREPARING.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.PREPARING.value: {
        InferencePhase.DEPLOYING.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.DEPLOYING.value: {
        InferencePhase.VERIFYING.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.VERIFYING.value: {
        InferencePhase.READY.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.READY.value: {
        InferencePhase.DEGRADED.value,
        InferencePhase.UPDATING.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.DEGRADED.value: {
        InferencePhase.READY.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.UPDATING.value: {
        InferencePhase.READY.value,
        InferencePhase.TERMINATING.value,
        InferencePhase.FAILED.value,
        InferencePhase.UNKNOWN.value,
    },
    InferencePhase.TERMINATING.value: set(),
    InferencePhase.FAILED.value: {
        InferencePhase.TERMINATING.value,
    },
    InferencePhase.UNKNOWN.value: {
        InferencePhase.TERMINATING.value,
    },
}

#: ``InferenceService`` 状态机单例（便于直接调用 ``validate`` / ``can``）。
INFERENCE_STATE_MACHINE = StateMachine(
    INFERENCE_STATES, INFERENCE_TRANSITIONS, name="InferenceService"
)


# ---------------------------------------------------------------------------
# 资源分配状态机
# ---------------------------------------------------------------------------

#: ``Allocation`` 的全部合法状态集合。
ALLOCATION_STATES: set[str] = {status.value for status in AllocationStatus}

#: ``Allocation`` 权威转移表：``Active → {Released, Expired}``，终态无出边。
ALLOCATION_TRANSITIONS: dict[str, set[str]] = {
    AllocationStatus.ACTIVE.value: {
        AllocationStatus.RELEASED.value,
        AllocationStatus.EXPIRED.value,
    },
    AllocationStatus.RELEASED.value: set(),
    AllocationStatus.EXPIRED.value: set(),
}

#: ``Allocation`` 状态机单例。
ALLOCATION_STATE_MACHINE = StateMachine(
    ALLOCATION_STATES, ALLOCATION_TRANSITIONS, name="Allocation"
)


__all__ = [
    "StateMachine",
    "INFERENCE_STATES",
    "INFERENCE_TRANSITIONS",
    "INFERENCE_STATE_MACHINE",
    "ALLOCATION_STATES",
    "ALLOCATION_TRANSITIONS",
    "ALLOCATION_STATE_MACHINE",
]
