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
* **结果查询**：不确定状态下应先查询真实状态，不得盲目重试不可幂等操作
  （:meth:`WorkflowRuntime.get_run` 返回步骤状态与输出，
  :meth:`WorkflowRuntime.find_run` 按幂等键、:meth:`WorkflowRuntime.find_runs` 按幂等键
  前缀查找而不创建运行）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .states import WORKFLOW_TRANSITIONS, StepStatus, WorkflowStatus


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
    needs: list[str] = Field(default_factory=list)
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    timeout: int | None = Field(default=None, description="步骤级超时（秒）")
    compensate: str | None = Field(default=None, description="补偿步骤 id（回滚时执行）")
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
    timeout_seconds: int | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step]


class RunRef(BaseModel):
    """运行实例引用（轻量、可持久化的定位符）。

    Attributes:
        run_id: 运行实例唯一标识。
        definition_version: 运行实例固定的定义版本号（版本固定语义，ADR-011）。
    """

    run_id: str
    definition_version: int


class RunInfo(BaseModel):
    """运行实例快照（只读）。

    Attributes:
        run_id: 运行实例标识。
        status: 运行状态。
        steps: 各步骤状态，按定义中的步骤顺序。
        outputs: 已成功步骤的输出 ``{step_id: result}``；步骤之后被补偿也保留，
            调用方据此取回步骤创建的外部对象（如实例 id）。
        error: 运行失败的原因（失败的步骤与错误信息）；运行没有失败时为 ``None``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: WorkflowStatus
    steps: dict[str, StepStatus] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @property
    def terminal(self) -> bool:
        """运行是否已到终态（之后不再变化）。"""
        return not WORKFLOW_TRANSITIONS[self.status]


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
        inputs: dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        """启动（或幂等复用）一个运行实例。

        返回时运行可能仍在执行，进度经 :meth:`get_status` / :meth:`get_run` 查询。

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

    async def get_run(self, run_id: str) -> RunInfo:
        """查询运行实例快照（状态、步骤状态、步骤输出与失败原因）。

        默认实现只经 :meth:`get_status` 填充 ``status``；持久化步骤结果的实现应覆盖。

        Raises:
            NotFoundError: 运行实例不存在（与 :meth:`get_status` 一致）。
        """
        return RunInfo(run_id=run_id, status=await self.get_status(run_id))

    async def find_run(self, idempotency_key: str) -> RunInfo | None:
        """按幂等键查找运行实例，不创建；不存在时返回 ``None``。

        与 :meth:`start` 不同，本方法没有副作用：调用方不确定运行是否存在时（如删除
        资源前取消进行中的部署）据此查询真实状态。默认实现抛出 :class:`NotImplementedError`。
        """
        raise NotImplementedError

    async def find_runs(self, key_prefix: str) -> dict[str, RunInfo]:
        """查找幂等键以 ``key_prefix`` 开头的全部运行实例，不创建。

        返回 ``{幂等键: 快照}``，按运行的创建先后排列；没有匹配时返回空字典。幂等键带
        版本号等后缀时（如 ``deployment:{uid}:{generation}``），调用方据此找到同一对象的
        所有运行。默认实现抛出 :class:`NotImplementedError`。
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
    async def signal(self, run_id: str, name: str, payload: dict[str, Any]) -> None:
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
    "RunInfo",
    "WorkflowRuntime",
]
