"""ForgeMesh 控制平面 — 推理服务部署（启动器契约、部署工作流、删除时的清理）。

* :mod:`.launcher` —— 启动器契约 :class:`RuntimeLauncher` 及其请求 / 结果模型；
* :mod:`.workflow` —— 部署工作流定义、幂等键、资源部署运行的查找
  :func:`find_deployment_runs` 与步骤 handler :class:`DeploymentSteps`；
* :mod:`.release` —— 删除资源或部署新的 spec 之前取消部署、停止实例的
  :class:`DeploymentReleaser`；
* :mod:`.recovery` —— 把实例已不在运行的 ``Ready`` 服务转为 ``Degraded`` 的
  :func:`check_instances`（控制平面启动时与运行中定期调用），以及回收归属资源已不存在的
  分配的 :func:`reclaim_orphaned_allocations`；
* :mod:`.fake` —— 在控制平面进程内为每个实例提供 OpenAI 兼容桩服务的
  :class:`FakeLauncher`（CI 与本地开发）；
* :mod:`.process` —— 本机进程启动器的公共部分（启动、就绪检查与停止）；
* :mod:`.vllm_process` —— 把每个实例作为本机 vLLM 子进程运行的 :class:`VLLMProcessLauncher`；
* :mod:`.tgi_process` —— 把每个实例作为本机 TGI 子进程运行的 :class:`TGIProcessLauncher`；
* :mod:`.config` —— 按环境变量（``FORGEMESH_RUNTIME_LAUNCHER`` 等）选择启动器的
  :func:`launcher_from_env` 与核对实例的间隔 :func:`instance_check_interval_from_env`；
* :mod:`.network` —— 启动器共用的地址校验与健康检查；
* :mod:`.memory` —— 测试替身 :class:`InMemoryLauncher`。

本包只依赖工作流契约（``forgemesh-workflow-api``），不依赖具体的工作流运行时。
"""

from __future__ import annotations

from .config import (
    INSTANCE_CHECK_INTERVAL_ENV,
    RUNTIME_LAUNCHER_ENV,
    LauncherConfigError,
    instance_check_interval_from_env,
    launcher_from_env,
)
from .fake import FakeLauncher
from .launcher import LaunchedInstance, LaunchRequest, RuntimeLauncher
from .memory import InMemoryLauncher
from .recovery import (
    DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS,
    INSTANCE_LOST_REASON,
    check_instances,
    reclaim_orphaned_allocations,
)
from .release import DeploymentReleaser
from .tgi_process import TGIProcessLauncher
from .vllm_process import VLLMProcessLauncher
from .workflow import (
    DEPLOYMENT_WORKFLOW,
    LAUNCH_INSTANCE,
    PREPARE_MODEL,
    VERIFY_INSTANCE,
    DeploymentRun,
    DeploymentSteps,
    StepCallable,
    deployment_inputs,
    deployment_run_key,
    deployment_workflow,
    find_deployment_runs,
    launched_instance,
)

__all__ = [
    "DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS",
    "DEPLOYMENT_WORKFLOW",
    "INSTANCE_CHECK_INTERVAL_ENV",
    "INSTANCE_LOST_REASON",
    "LAUNCH_INSTANCE",
    "PREPARE_MODEL",
    "RUNTIME_LAUNCHER_ENV",
    "VERIFY_INSTANCE",
    "DeploymentReleaser",
    "DeploymentRun",
    "DeploymentSteps",
    "FakeLauncher",
    "InMemoryLauncher",
    "LaunchRequest",
    "LaunchedInstance",
    "LauncherConfigError",
    "RuntimeLauncher",
    "StepCallable",
    "TGIProcessLauncher",
    "VLLMProcessLauncher",
    "check_instances",
    "deployment_inputs",
    "deployment_run_key",
    "deployment_workflow",
    "find_deployment_runs",
    "instance_check_interval_from_env",
    "launched_instance",
    "launcher_from_env",
    "reclaim_orphaned_allocations",
]
