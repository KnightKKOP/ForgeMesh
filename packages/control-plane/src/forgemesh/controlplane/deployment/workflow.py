"""推理服务的部署工作流：准备模型 → 启动实例 → 验证实例。

协调器以 :func:`deployment_run_key` 为幂等键启动工作流（资源 uid 的每一代 spec 一次运行，
实例丢失后重新部署时同一代 spec 再启动一次，:func:`find_deployment_runs` 找回资源的全部
运行），运行输入是 :func:`deployment_inputs` 包装的
:class:`~forgemesh.controlplane.deployment.launcher.LaunchRequest`。各步骤经
:class:`DeploymentSteps` 调用 :class:`~forgemesh.controlplane.deployment.launcher.RuntimeLauncher`：

``prepare-model``
    ``launcher.prepare(request)``，结果为空。
``launch-instance``
    ``launcher.launch(request)``，结果是 :class:`LaunchedInstance` 的 JSON 形式；补偿为
    ``launcher.stop(instance_id)``。
``verify-instance``
    ``launcher.verify(instance)``，检查 ``launch-instance`` 启动的实例，结果为空。

某一步用尽重试后，工作流运行时补偿已成功的 ``launch-instance``（停止实例），运行以
``Failed`` 结束；:func:`launched_instance` 从运行快照中取出可能仍在运行的实例。

工作流运行时（``forgemesh-workflow-runtime``，控制平面本身只依赖工作流契约）这样组装::

    steps = DeploymentSteps(launcher)
    compensation = CompensationExecutor()
    for step_id, handler in steps.compensations().items():
        compensation.registry.register(step_id, handler)
    runtime = LocalWorkflowRuntime(
        store=WorkflowStore(workflow_db_path(db_path)),
        executor=WorkflowExecutor(),
        compensation=compensation,
        approval=ApprovalGate(),
        handler=steps.handle,
    )
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from forgemesh.workflow.runtime import RunInfo, Step, WorkflowDefinition, WorkflowRuntime
from forgemesh.workflow.states import StepStatus

from .launcher import LaunchedInstance, LaunchRequest, RuntimeLauncher

#: 部署工作流的名称。
DEPLOYMENT_WORKFLOW = "inference-service-deployment"

PREPARE_MODEL = "prepare-model"
LAUNCH_INSTANCE = "launch-instance"
VERIFY_INSTANCE = "verify-instance"

#: 各步骤单次尝试的时限（秒）。下载模型可能很慢；验证包含实例加载模型的时间。
PREPARE_TIMEOUT_SECONDS = 3600
LAUNCH_TIMEOUT_SECONDS = 600
VERIFY_TIMEOUT_SECONDS = 1800

#: 步骤与补偿 handler 的类型：``async (step, inputs) -> dict``。
StepCallable = Callable[[Step, dict[str, Any]], Awaitable[dict[str, Any]]]

_USES_PREFIX = "deployment."
_RUN_KEY_PREFIX = "deployment:"
#: ``launch-instance`` 处于这些状态时，它启动的实例可能仍在运行。
_INSTANCE_MAY_RUN = frozenset(
    {StepStatus.SUCCEEDED, StepStatus.COMPENSATING, StepStatus.COMPENSATION_FAILED}
)


def deployment_workflow() -> WorkflowDefinition:
    """部署工作流定义（每次返回新对象；不设工作流超时，由各步骤的时限约束）。"""
    return WorkflowDefinition(
        name=DEPLOYMENT_WORKFLOW,
        steps=[
            Step(
                id=PREPARE_MODEL,
                uses=_USES_PREFIX + PREPARE_MODEL,
                timeout=PREPARE_TIMEOUT_SECONDS,
            ),
            Step(
                id=LAUNCH_INSTANCE,
                uses=_USES_PREFIX + LAUNCH_INSTANCE,
                needs=[PREPARE_MODEL],
                timeout=LAUNCH_TIMEOUT_SECONDS,
            ),
            Step(
                id=VERIFY_INSTANCE,
                uses=_USES_PREFIX + VERIFY_INSTANCE,
                needs=[LAUNCH_INSTANCE],
                timeout=VERIFY_TIMEOUT_SECONDS,
            ),
        ],
    )


def deployment_run_key(uid: str, generation: int, attempt: int = 1) -> str:
    """资源 ``uid`` 第 ``generation`` 代 spec 第 ``attempt`` 次部署的运行的幂等键，也是实例的
    幂等键（``LaunchRequest.instance_key``）：第一次形如 ``deployment:{uid}:{generation}``，
    之后（实例丢失后重新部署）形如 ``deployment:{uid}:{generation}:{attempt}``。

    spec 变化（新的 generation）与重新部署都启动新的运行与新的实例；删除资源或部署之前，
    :func:`find_deployment_runs` 找回资源的全部运行，旧实例不会无人管理。

    Raises:
        ValueError: ``attempt`` 小于 1。
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    key = f"{_RUN_KEY_PREFIX}{uid}:{generation}"
    return key if attempt == 1 else f"{key}:{attempt}"


@dataclass(frozen=True)
class DeploymentRun:
    """资源的一次部署运行。

    Attributes:
        key: 运行的幂等键。
        generation: 运行部署的 spec 的 generation；1.1.0 之前的运行（键为
            ``deployment:{uid}``，不含 generation）为 ``None``。
        run: 运行快照。
        attempt: 这一代 spec 的第几次部署（1.1.0 之前的运行为 1）。
    """

    key: str
    generation: int | None
    run: RunInfo
    attempt: int = 1


async def find_deployment_runs(
    runtime: WorkflowRuntime, uid: str, generation: int | None = None
) -> list[DeploymentRun]:
    """资源 ``uid`` 的部署运行（不创建），按创建先后排列。

    经 :meth:`~forgemesh.workflow.runtime.WorkflowRuntime.find_runs` 按幂等键前缀查找；给出
    ``generation`` 时只返回部署这一代 spec 的运行（各次部署），否则返回全部运行，包括 1.1.0
    之前的 ``deployment:{uid}``。
    """
    prefix = f"{_RUN_KEY_PREFIX}{uid}"
    pattern = re.compile(rf"{re.escape(prefix)}(?::(\d+)(?::(\d+))?)?", re.ASCII)
    if generation is not None:
        prefix = f"{prefix}:{generation}"
    runs = []
    for key, run in (await runtime.find_runs(prefix)).items():
        match = pattern.fullmatch(key)
        if match is None:
            continue
        found = int(match[1]) if match[1] is not None else None
        attempt = int(match[2]) if match[2] is not None else 1
        if generation is None or found == generation:
            runs.append(DeploymentRun(key=key, generation=found, run=run, attempt=attempt))
    return runs


def deployment_inputs(request: LaunchRequest) -> dict[str, Any]:
    """部署工作流的运行输入。"""
    return {"request": request.model_dump(mode="json")}


def launched_instance(run: RunInfo) -> LaunchedInstance | None:
    """运行启动的、可能仍在运行的实例：``launch-instance`` 成功过且没有补偿成功时返回。"""
    output = run.outputs.get(LAUNCH_INSTANCE)
    if run.steps.get(LAUNCH_INSTANCE) not in _INSTANCE_MAY_RUN or output is None:
        return None
    return LaunchedInstance.model_validate(output)


class DeploymentSteps:
    """部署工作流的步骤 handler 与补偿 handler，交给工作流运行时调用。

    Args:
        launcher: 执行各步骤的启动器。
    """

    def __init__(self, launcher: RuntimeLauncher) -> None:
        self._launcher = launcher

    async def handle(self, step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        """步骤 handler：按 ``step.uses`` 调用启动器，返回步骤结果。

        Raises:
            ValueError: 不是部署工作流的步骤。
        """
        if step.uses == _USES_PREFIX + PREPARE_MODEL:
            await self._launcher.prepare(LaunchRequest.model_validate(inputs["request"]))
            return {}
        if step.uses == _USES_PREFIX + LAUNCH_INSTANCE:
            request = LaunchRequest.model_validate(inputs["request"])
            return (await self._launcher.launch(request)).model_dump(mode="json")
        if step.uses == _USES_PREFIX + VERIFY_INSTANCE:
            await self._launcher.verify(LaunchedInstance.model_validate(inputs[LAUNCH_INSTANCE]))
            return {}
        raise ValueError(f"not a deployment step: {step.uses!r}")

    async def stop_instance(self, step: Step, result: dict[str, Any]) -> dict[str, Any]:
        """``launch-instance`` 的补偿：停止该步骤启动的实例（参数为该步骤的结果）。"""
        await self._launcher.stop(LaunchedInstance.model_validate(result).instance_id)
        return {}

    def compensations(self) -> dict[str, StepCallable]:
        """要注册到补偿执行器的补偿 handler（按步骤 id）。"""
        return {LAUNCH_INSTANCE: self.stop_instance}


__all__ = [
    "DEPLOYMENT_WORKFLOW",
    "LAUNCH_INSTANCE",
    "LAUNCH_TIMEOUT_SECONDS",
    "PREPARE_MODEL",
    "PREPARE_TIMEOUT_SECONDS",
    "VERIFY_INSTANCE",
    "VERIFY_TIMEOUT_SECONDS",
    "DeploymentRun",
    "DeploymentSteps",
    "StepCallable",
    "deployment_inputs",
    "deployment_run_key",
    "deployment_workflow",
    "find_deployment_runs",
    "launched_instance",
]
