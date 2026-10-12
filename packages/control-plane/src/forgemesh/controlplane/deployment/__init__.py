"""ForgeMesh 控制平面 — 推理服务部署（启动器契约、部署工作流、删除时的清理）。

* :mod:`.launcher` —— 启动器契约 :class:`RuntimeLauncher` 及其请求 / 结果模型；
* :mod:`.workflow` —— 部署工作流定义、幂等键与步骤 handler :class:`DeploymentSteps`；
* :mod:`.release` —— 删除资源时取消部署、停止实例的 :class:`DeploymentReleaser`；
* :mod:`.memory` —— 测试替身 :class:`InMemoryLauncher`。

本包只依赖工作流契约（``forgemesh-workflow-api``），不依赖具体的工作流运行时。
"""

from __future__ import annotations

from .launcher import LaunchedInstance, LaunchRequest, RuntimeLauncher
from .memory import InMemoryLauncher
from .release import DeploymentReleaser
from .workflow import (
    DEPLOYMENT_WORKFLOW,
    LAUNCH_INSTANCE,
    PREPARE_MODEL,
    VERIFY_INSTANCE,
    DeploymentSteps,
    StepCallable,
    deployment_inputs,
    deployment_run_key,
    deployment_workflow,
    launched_instance,
)

__all__ = [
    "DEPLOYMENT_WORKFLOW",
    "LAUNCH_INSTANCE",
    "PREPARE_MODEL",
    "VERIFY_INSTANCE",
    "DeploymentReleaser",
    "DeploymentSteps",
    "InMemoryLauncher",
    "LaunchRequest",
    "LaunchedInstance",
    "RuntimeLauncher",
    "StepCallable",
    "deployment_inputs",
    "deployment_run_key",
    "deployment_workflow",
    "launched_instance",
]
