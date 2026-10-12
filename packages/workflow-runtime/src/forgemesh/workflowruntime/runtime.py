"""进程内工作流运行时：在后台执行运行实例，逐步落盘，失败时补偿，重启后恢复。

:class:`LocalWorkflowRuntime` 组合 :class:`~forgemesh.workflowruntime.store.WorkflowStore`
（持久化）、:class:`~forgemesh.workflowruntime.exec.WorkflowExecutor`（重试、步骤超时、
工作流超时）、:class:`~forgemesh.workflowruntime.compensate.CompensationExecutor`
（反向补偿）与 :class:`~forgemesh.workflowruntime.approval.ApprovalGate`（审批），
实现 :class:`forgemesh.workflow.runtime.WorkflowRuntime` 契约。

执行语义
--------
* **后台执行**：``start`` 创建（或按幂等键复用）运行实例、推进到 ``Running`` 后立即
  返回；步骤由运行时持有的后台任务按定义顺序执行，每一步的状态与结果都落盘。全部
  步骤成功 → ``Succeeded``。步骤的输入是运行输入加上已成功步骤的结果（按步骤 id）。
* **失败**：某一步用尽重试策略（或工作流超时）后，运行进入 ``Compensating``，按反向
  顺序补偿已成功且注册了补偿的步骤，然后进入 ``Failed``；``error`` 记录失败的步骤与
  错误。失败的步骤本身不补偿（handler 失败或被取消时须自行清理），未执行的步骤置为
  ``Skipped``。工作流超时也按失败处理，不使用 ``TimedOut``。
* **取消**：``cancel`` 中断正在执行的步骤，等待运行进入 ``Cancelled``；``cleanup=True``
  时先补偿已成功的步骤。已结束的运行不受影响。
* **恢复**：``recover`` 为存储中所有未结束的运行重新启动后台任务。从第一个未成功的
  步骤继续，中断时正在执行的步骤会以相同的输入再执行一次，handler 须保证这不会产生
  第二份副作用（handler 拿不到运行 id，幂等所需的标识须放在运行输入中）；中断时正在
  补偿或取消的运行继续完成补偿。工作流超时从恢复时重新计时。
* **关闭**：``aclose`` 之后不再启动新的执行。正在执行步骤的后台任务被取消，运行保持
  未结束，下次 ``recover`` 时继续；正在补偿的任务最多等待 ``timeout`` 秒。
* **审批**：``requires_approval`` 的步骤开始前运行进入 ``Waiting``，收到
  ``signal("approve" | "reject", {"step_id": ...})`` 后继续。拒绝视为该步骤失败（按重试
  策略重试时立即再次失败）。审批结果只保存在内存中，重启后需要重新审批。

只能在单个事件循环中使用。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from forgemesh.core.errors import InvalidStateTransition
from forgemesh.workflow.runtime import RunInfo, RunRef, Step, WorkflowDefinition, WorkflowRuntime
from forgemesh.workflow.states import StepStatus, WorkflowStatus

from .approval import ApprovalGate
from .compensate import CompensationExecutor
from .exec import WorkflowExecutor
from .store import WorkflowStore

logger = logging.getLogger(__name__)

#: 步骤 handler 类型：``async (step, inputs) -> dict``。
StepHandler = Callable[[Step, dict[str, Any]], Awaitable[dict[str, Any]]]

_TERMINAL = frozenset(
    {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMEDOUT,
    }
)
_IN_FLIGHT_STEPS = frozenset({StepStatus.RUNNING, StepStatus.RETRYING})
#: 运行未结束时，步骤处于这些状态之外说明中断前已开始收尾（失败或取消）。
_RESUMABLE_STEPS = frozenset(
    {StepStatus.PENDING, StepStatus.RUNNING, StepStatus.RETRYING, StepStatus.SUCCEEDED}
)
#: 成功过的步骤（之后可能被补偿），其结果作为运行输出返回。
_OUTPUT_STEPS = frozenset(
    {
        StepStatus.SUCCEEDED,
        StepStatus.COMPENSATING,
        StepStatus.COMPENSATED,
        StepStatus.COMPENSATION_FAILED,
    }
)


class ApprovalRejectedError(Exception):
    """步骤的审批被拒绝。"""


@dataclass
class _Execution:
    """一个运行实例的后台执行。"""

    task: asyncio.Task[None] | None = None
    started: bool = False
    cancel_requested: bool = False
    cleanup: bool = False
    #: 已进入收尾（补偿与写终态）：取消与关闭都等待它完成，而不是中断它。
    finishing: bool = False
    current_step: str | None = None


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


async def _durably(write: Awaitable[None]) -> None:
    """执行一次写入；调用方被取消时先等写入完成再传播取消，后续读取不会看到旧状态。"""
    future = asyncio.ensure_future(write)
    try:
        await asyncio.shield(future)
    except asyncio.CancelledError:
        await asyncio.wait({future})
        if not future.cancelled():
            future.exception()
        raise


class LocalWorkflowRuntime(WorkflowRuntime):
    """在当前进程的事件循环中执行工作流的运行时（语义见模块说明）。

    Args:
        store: 工作流持久化存储；运行时不负责关闭它（先 :meth:`aclose` 再关闭存储）。
        executor: 工作流执行器（重试策略在其 ``StepExecutor`` 上配置）。
        compensation: 补偿执行器，补偿 handler 按步骤 id 注册，参数为该步骤的结果。
        approval: 审批门控。
        handler: 步骤执行 handler ``async (step, inputs) -> dict``，返回值为步骤结果
            （须可 JSON 序列化）。
    """

    def __init__(
        self,
        store: WorkflowStore,
        executor: WorkflowExecutor,
        compensation: CompensationExecutor,
        approval: ApprovalGate,
        handler: StepHandler,
    ) -> None:
        self._store = store
        self._executor = executor
        self._compensation = compensation
        self._approval = approval
        self._handler = handler
        self._executions: dict[str, _Execution] = {}
        self._closed = False

    # ------------------------------------------------------------------ #
    # WorkflowRuntime 契约
    # ------------------------------------------------------------------ #

    async def start(
        self,
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        """创建（或按幂等键复用）运行实例，推进到 ``Running`` 并在后台执行。

        同一 ``idempotency_key`` 重复调用返回同一运行，不会再执行一次：运行正在执行时
        直接返回；已结束时原样返回；未结束且没有在执行（如重启后尚未 ``recover``）时
        继续执行。

        Raises:
            RuntimeError: 运行时已关闭。
        """
        self._ensure_open()
        ref = await self._store.create_run(defn, inputs, idempotency_key)
        run = await self._store.get_run(ref.run_id)
        if run["status"] == WorkflowStatus.PENDING.value:
            # 并发的 start 可能已推进过。
            with contextlib.suppress(InvalidStateTransition):
                await self._store.update_run_status(ref.run_id, WorkflowStatus.RUNNING.value)
        elif WorkflowStatus(run["status"]) in _TERMINAL:
            return ref
        self._spawn(ref.run_id)
        return ref

    async def get_status(self, run_id: str) -> WorkflowStatus:
        """查询运行实例当前状态。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        run = await self._store.get_run(run_id)
        return WorkflowStatus(run["status"])

    async def get_run(self, run_id: str) -> RunInfo:
        """查询运行实例快照（步骤按定义顺序，输出为成功过的步骤的结果）。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        return self._snapshot(await self._store.get_run(run_id))

    async def find_run(self, idempotency_key: str) -> RunInfo | None:
        """按幂等键查找运行实例（不创建）；不存在时返回 ``None``。"""
        run = await self._store.get_run_by_idempotency_key(idempotency_key)
        return None if run is None else self._snapshot(run)

    async def cancel(self, run_id: str, cleanup: bool = False) -> None:
        """取消运行实例并等待它进入终态；已结束的运行直接返回。

        正在执行的步骤被中断（handler 收到 :class:`asyncio.CancelledError`），未执行的
        步骤置为 ``Cancelled``；``cleanup=True`` 时按反向顺序补偿已成功的步骤。运行
        正在因失败而补偿时，等待它以 ``Failed`` 结束。

        Raises:
            NotFoundError: 运行实例不存在。
            RuntimeError: 运行时已关闭，或取消未能完成（运行仍未结束，可再次调用）。
        """
        if WorkflowStatus((await self._store.get_run(run_id))["status"]) in _TERMINAL:
            return
        self._ensure_open()
        execution = self._executions.get(run_id)
        if execution is None:
            execution = self._spawn(run_id, _Execution(cancel_requested=True, cleanup=cleanup))
        elif not execution.finishing:
            execution.cleanup = execution.cleanup or cleanup
            if not execution.cancel_requested:
                execution.cancel_requested = True
                if execution.started and execution.task is not None:
                    execution.task.cancel()
        if execution.task is not None:
            await asyncio.wait({execution.task})
        status = await self.get_status(run_id)
        if status not in _TERMINAL:
            raise RuntimeError(f"workflow run {run_id} did not finish cancelling: {status.value}")

    async def signal(self, run_id: str, name: str, payload: dict[str, Any]) -> None:
        """向运行实例投递审批信号，唤醒处于 ``Waiting`` 的步骤。

        支持的信号名称：``"approve"``（审批通过）与 ``"reject"``（审批拒绝）。信号先于
        等待到达时保留，步骤开始等待时立即生效。

        Args:
            run_id: 运行实例标识。
            name: 信号名称。
            payload: 信号载荷，须包含 ``step_id``。

        Raises:
            NotFoundError: 运行实例不存在。
            ValueError: 不支持的信号名称或缺少 ``step_id``。
        """
        await self._store.get_run(run_id)

        step_id = payload.get("step_id")
        if not step_id:
            raise ValueError("signal payload must contain 'step_id'")

        if name == "approve":
            self._approval.approve(run_id, step_id)
        elif name == "reject":
            self._approval.reject(run_id, step_id)
        else:
            raise ValueError(f"unsupported signal: {name!r}")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def recover(self) -> list[str]:
        """为存储中所有未结束的运行启动后台执行（进程启动时调用一次），返回其 id。

        Raises:
            RuntimeError: 运行时已关闭。
        """
        self._ensure_open()
        run_ids = [run["run_id"] for run in await self._store.list_pending_runs()]
        for run_id in run_ids:
            self._spawn(run_id)
        if run_ids:
            logger.info("resuming %d unfinished workflow run(s)", len(run_ids))
        return run_ids

    async def wait(self, run_id: str) -> RunInfo:
        """等待运行实例当前的后台执行结束（没有在执行时立即返回），返回运行快照。

        Raises:
            NotFoundError: 运行实例不存在。
        """
        execution = self._executions.get(run_id)
        if execution is not None and execution.task is not None:
            await asyncio.wait({execution.task})
        return await self.get_run(run_id)

    async def aclose(self, timeout: float = 10.0) -> None:
        """停止全部后台执行；之后 ``start`` / ``cancel`` / ``recover`` 抛 :class:`RuntimeError`。

        正在执行步骤的任务立即取消（运行保持未结束，重启后由 ``recover`` 继续）；正在
        收尾的任务最多等待 ``timeout`` 秒，超时后取消（同样可在重启后继续）。返回时不再
        有进行中的写入，可以关闭存储。
        """
        self._closed = True
        executions = list(self._executions.values())
        for execution in executions:
            if execution.task is not None and not (
                execution.finishing or execution.cancel_requested
            ):
                execution.task.cancel()
        tasks = {execution.task for execution in executions if execution.task is not None}
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            logger.warning(
                "workflow task %s did not finish in %.1fs; cancelling", task.get_name(), timeout
            )
            task.cancel()
        if pending:
            await asyncio.wait(pending)

    # ------------------------------------------------------------------ #
    # 后台执行
    # ------------------------------------------------------------------ #

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("the workflow runtime is closed")

    def _spawn(self, run_id: str, execution: _Execution | None = None) -> _Execution:
        """为运行启动后台任务；已在执行时返回现有的执行。"""
        current = self._executions.get(run_id)
        if current is not None:
            return current
        execution = execution if execution is not None else _Execution()
        if self._closed:
            return execution
        task = asyncio.create_task(
            self._execute(run_id, execution), name=f"forgemesh-workflow-{run_id}"
        )
        execution.task = task
        self._executions[run_id] = execution
        task.add_done_callback(functools.partial(self._forget, run_id, execution))
        return execution

    def _forget(self, run_id: str, execution: _Execution, task: asyncio.Task[None]) -> None:
        if self._executions.get(run_id) is execution:
            del self._executions[run_id]
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error(
                "workflow run %s stopped before reaching a final state; it is resumed by "
                "the next recover()",
                run_id,
                exc_info=exc,
            )

    async def _execute(self, run_id: str, execution: _Execution) -> None:
        execution.started = True
        if not execution.cancel_requested:
            try:
                await self._advance(run_id, execution)
                return
            except asyncio.CancelledError:
                if not execution.cancel_requested:
                    raise
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
            except Exception as exc:  # noqa: BLE001 - 任何失败都要补偿并落成 Failed
                error = _describe(exc)
                if execution.current_step is not None:
                    error = f"step {execution.current_step!r} failed: {error}"
                logger.warning("workflow run %s failed: %s", run_id, error)
                execution.finishing = True
                await self._finish(run_id, WorkflowStatus.FAILED, cleanup=True, error=error)
                return
        execution.finishing = True
        await self._finish(run_id, WorkflowStatus.CANCELLED, cleanup=execution.cleanup, error=None)

    async def _advance(self, run_id: str, execution: _Execution) -> None:
        """从存储中的状态继续执行运行：执行剩余步骤，或完成中断前已开始的收尾。"""
        run = await self._store.get_run(run_id)
        status = WorkflowStatus(run["status"])
        if status in _TERMINAL:
            return
        defn = WorkflowDefinition.model_validate_json(run["definition_json5"])
        rows = {row["step_id"]: row for row in run["steps"]}
        statuses = {step.id: StepStatus(rows[step.id]["status"]) for step in defn.steps}

        if status is WorkflowStatus.COMPENSATING or not set(statuses.values()) <= _RESUMABLE_STEPS:
            error = run["error"] or _recorded_failure(rows, statuses)
            failed = error is not None
            execution.finishing = True
            await self._finish(
                run_id,
                WorkflowStatus.FAILED if failed else WorkflowStatus.CANCELLED,
                cleanup=failed or status is WorkflowStatus.COMPENSATING,
                error=error,
            )
            return

        if status is WorkflowStatus.PENDING:
            await _durably(self._store.update_run_status(run_id, WorkflowStatus.RUNNING.value))
        results = {
            step_id: rows[step_id]["result"]
            for step_id, step_status in statuses.items()
            if step_status is StepStatus.SUCCEEDED
        }
        remaining = [step for step in defn.steps if statuses[step.id] is not StepStatus.SUCCEEDED]
        await self._executor.execute_workflow(
            defn.model_copy(update={"steps": remaining}),
            {**defn.inputs, **run["inputs"], **results},
            run_id,
            functools.partial(self._run_step, run_id, execution),
        )
        execution.current_step = None
        if WorkflowStatus((await self._store.get_run(run_id))["status"]) is WorkflowStatus.WAITING:
            await _durably(self._store.update_run_status(run_id, WorkflowStatus.RUNNING.value))
        await _durably(self._store.update_run_status(run_id, WorkflowStatus.SUCCEEDED.value))
        logger.info("workflow run %s succeeded", run_id)

    async def _run_step(
        self, run_id: str, execution: _Execution, step: Step, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        """执行一次步骤尝试（由 ``StepExecutor`` 按重试策略调用）并落盘状态与结果。"""
        execution.current_step = step.id
        run = await self._store.get_run(run_id)
        run_status = WorkflowStatus(run["status"])
        status = next(
            StepStatus(row["status"]) for row in run["steps"] if row["step_id"] == step.id
        )

        if status is StepStatus.PENDING and step.requires_approval:
            if run_status is WorkflowStatus.RUNNING:
                await _durably(self._store.update_run_status(run_id, WorkflowStatus.WAITING.value))
                run_status = WorkflowStatus.WAITING
            if not await self._approval.wait_for_approval(run_id, step.id):
                raise ApprovalRejectedError(f"approval of step {step.id!r} was rejected")
        if run_status is WorkflowStatus.WAITING:
            await _durably(self._store.update_run_status(run_id, WorkflowStatus.RUNNING.value))
        if status is StepStatus.RUNNING:
            # 上一次尝试被中断（重启或步骤超时），没有留下结果。
            await _durably(
                self._store.update_step_status(run_id, step.id, StepStatus.RETRYING.value)
            )
        await _durably(self._store.update_step_status(run_id, step.id, StepStatus.RUNNING.value))
        try:
            result = await self._handler(step, inputs)
        except Exception as exc:
            await _durably(
                self._store.update_step_status(
                    run_id, step.id, StepStatus.RETRYING.value, {"error": _describe(exc)}
                )
            )
            raise
        await _durably(
            self._store.update_step_status(
                run_id, step.id, StepStatus.SUCCEEDED.value, {} if result is None else result
            )
        )
        return result

    async def _finish(
        self,
        run_id: str,
        outcome: WorkflowStatus,
        *,
        cleanup: bool,
        error: str | None,
    ) -> None:
        """把运行收尾为 ``outcome``（``Failed`` 或 ``Cancelled``），必要时先补偿。

        按存储中的状态推进，可重复执行：中断后再次调用从中断处继续。
        """
        run = await self._store.get_run(run_id)
        status = WorkflowStatus(run["status"])
        if status in _TERMINAL:
            return
        defn = WorkflowDefinition.model_validate_json(run["definition_json5"])
        rows = {row["step_id"]: row for row in run["steps"]}
        failed = outcome is WorkflowStatus.FAILED
        compensate = failed or cleanup

        if compensate and status is not WorkflowStatus.COMPENSATING:
            if status is not WorkflowStatus.RUNNING:
                await _durably(self._store.update_run_status(run_id, WorkflowStatus.RUNNING.value))
            await _durably(
                self._store.update_run_status(run_id, WorkflowStatus.COMPENSATING.value, error)
            )

        for step in defn.steps:
            current = StepStatus(rows[step.id]["status"])
            if current in _IN_FLIGHT_STEPS:
                target, result = (
                    (StepStatus.FAILED, {"error": error})
                    if failed
                    else (StepStatus.CANCELLED, None)
                )
            elif current is StepStatus.PENDING:
                target, result = (StepStatus.SKIPPED if failed else StepStatus.CANCELLED), None
            else:
                continue
            await _durably(self._store.update_step_status(run_id, step.id, target.value, result))

        if compensate:
            for step in reversed(defn.steps):
                current = StepStatus(rows[step.id]["status"])
                if current not in (
                    StepStatus.SUCCEEDED,
                    StepStatus.COMPENSATING,
                ) or not self._compensation.registry.has_compensation(step.id):
                    continue
                if current is StepStatus.SUCCEEDED:
                    await _durably(
                        self._store.update_step_status(
                            run_id, step.id, StepStatus.COMPENSATING.value
                        )
                    )
                result = rows[step.id]["result"]
                outcomes = await self._compensation.compensate(
                    defn, [step.id], {step.id: result if result is not None else {}}
                )
                await _durably(
                    self._store.update_step_status(run_id, step.id, outcomes[step.id].value)
                )

        await _durably(self._store.update_run_status(run_id, outcome.value, error))
        logger.info("workflow run %s finished: %s", run_id, outcome.value)

    @staticmethod
    def _snapshot(run: dict[str, Any]) -> RunInfo:
        defn = WorkflowDefinition.model_validate_json(run["definition_json5"])
        rows = {row["step_id"]: row for row in run["steps"]}
        steps = {step.id: StepStatus(rows[step.id]["status"]) for step in defn.steps}
        outputs = {
            step_id: rows[step_id]["result"]
            for step_id, status in steps.items()
            if status in _OUTPUT_STEPS and rows[step_id]["result"] is not None
        }
        return RunInfo(
            run_id=run["run_id"],
            status=run["status"],
            steps=steps,
            outputs=outputs,
            error=run["error"],
        )


def _recorded_failure(
    rows: dict[str, dict[str, Any]], statuses: dict[str, StepStatus]
) -> str | None:
    """中断前已失败的步骤对应的错误；没有失败的步骤时返回 ``None``。"""
    for step_id, status in statuses.items():
        if status is StepStatus.FAILED:
            result = rows[step_id]["result"]
            detail = result.get("error") if isinstance(result, dict) else None
            return detail or f"step {step_id!r} failed"
    return None


__all__ = ["ApprovalRejectedError", "LocalWorkflowRuntime", "StepHandler"]
