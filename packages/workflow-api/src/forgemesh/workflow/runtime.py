"""ForgeMesh WorkEngine 运行契约（对齐 development-plan 5.4 / ADR-003）。

本模块定义**引擎无关**的工作流定义与运行契约。任何执行引擎（参考执行器、
Temporal/Cadence 等）都必须满足同一语义，业务模型不与之耦合。

执行语义（必须由实现全部落地，对齐 architecture 6.4）：

* **持久化**：运行实例与步骤历史持久化，进程重启后可恢复。
* **幂等**：同一 ``idempotency_key`` 只应存在一个运行实例，重复请求不产生重复副作用。
* **重试**：依据 :class:`RetryPolicy` 按错误类型/次数/退避/时限重试。
* **超时**：区分步骤超时、工作流超时、外部任务超时。
* **取消**：``cleanup=False`` 仅停止后续步骤；``cleanup=True`` 执行补偿清理既有副作用。
* **补偿**：失败的运行按反向补偿步骤回滚已产生的副作用。
* **审批**：等待人工或外部事件的运行进入 ``WAITING``，由外部信号唤醒。
* **版本固定**：运行实例固定其 ``definition_version``，升级定义不影响既有实例。
* **结果查询**：不确定状态下应先查询真实状态，不得盲目重试不可幂等操作。

兼容 Python 3.9+。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .states import WorkflowStatus


class Step(BaseModel):
    """工作流中的单个步骤定义。

    Attributes:
        id: 步骤唯一标识（在所属工作流定义内唯一）。
        uses: 能力标识（如 ``gpu.discovery``），由具体 Provider 解释执行。
        needs: 依赖的前置步骤 id 列表，形成有向无环依赖图。
        with_: 步骤输入参数（``with`` 为 Python 保留字，故字段名带尾下划线，
            alias 为 ``with``，配合 ``populate_by_name`` 允许两种访问方式）。
        timeout: 步骤级超时（秒）。
        compensate: 补偿步骤 id（回滚时执行）。
        requires_approval: 是否需要人工审批。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    uses: str
    needs: List[str] = Field(default_factory=list)
    with_: Dict[str, Any] = Field(default_factory=dict, alias="with")
    timeout: Optional[int] = Field(default=None, description="步骤级超时（秒）")
    compensate: Optional[str] = Field(default=None, description="补偿步骤 id（回滚时执行）")
    requires_approval: bool = Field(default=False, description="是否需要人工审批")


class RetryPolicy(BaseModel):
    """步骤重试策略（对齐 development-plan 5.4「重试」语义）。

    Attributes:
        max_attempts: 最大尝试次数（含首次），至少为 1。
        initial_backoff_seconds: 初始退避时长（秒），alias ``backoffSeconds``。
        max_backoff_seconds: 退避时长上限（秒），指数退避时封顶。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_attempts: int = Field(default=3, ge=1)
    initial_backoff_seconds: float = Field(default=2.0, ge=0.0, alias="backoffSeconds")
    max_backoff_seconds: float = Field(default=60.0, ge=0.0)


class WorkflowDefinition(BaseModel):
    """工作流定义（不可变、可版本化）。

    Attributes:
        name: 定义名称。
        timeout_seconds: 工作流级整体超时（秒）；``None`` 表示不限时。
        inputs: 定义声明的输入默认值。
        steps: 步骤列表。
    """

    name: str
    timeout_seconds: Optional[int] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    steps: List[Step]


class RunRef(BaseModel):
    """运行实例引用（轻量、可持久化的定位符）。

    Attributes:
        run_id: 运行实例唯一标识。
        definition_version: 运行实例固定的定义版本号（版本固定语义，ADR-011）。
    """

    run_id: str
    definition_version: int


class WorkflowRuntime(ABC):
    """工作流运行契约（ADR-003：可替换 Provider，语义不随引擎改变）。

    实现必须满足 :mod:`forgemesh.workflow.runtime` 模块 docstring 中列出的
    持久化 / 幂等 / 重试 / 超时 / 取消 / 补偿 / 审批 / 版本固定语义。
    该契约**不产出资源终态**：任何状态写入必须经 Registry 由 Controller 完成。
    """

    @abstractmethod
    async def start(
        self,
        defn: WorkflowDefinition,
        inputs: Dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        """启动（或幂等复用）一个运行实例。

        Args:
            defn: 工作流定义（运行实例将固定其当前版本）。
            inputs: 运行时输入，覆盖/补充 ``defn.inputs``。
            idempotency_key: 幂等键；同一键重复调用不得产生重复副作用。

        Returns:
            指向既有或新建运行实例的 :class:`RunRef`。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_status(self, run_id: str) -> WorkflowStatus:
        """查询运行实例当前状态。

        不确定状态下应先查询真实状态再决策，不得盲目重试不可幂等操作。
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, run_id: str, cleanup: bool = False) -> None:
        """取消运行实例。

        Args:
            run_id: 运行实例标识。
            cleanup: ``False`` 仅停止后续步骤；``True`` 额外执行补偿以清理既有副作用。
        """
        raise NotImplementedError

    @abstractmethod
    async def signal(
        self, run_id: str, name: str, payload: Dict[str, Any]
    ) -> None:
        """向运行实例投递外部信号，唤醒处于 ``WAITING`` 的步骤/审批。

        Args:
            run_id: 运行实例标识。
            name: 信号名称（如审批结果、外部事件类型）。
            payload: 信号载荷。
        """
        raise NotImplementedError


__all__ = [
    "Step",
    "RetryPolicy",
    "WorkflowDefinition",
    "RunRef",
    "WorkflowRuntime",
]
