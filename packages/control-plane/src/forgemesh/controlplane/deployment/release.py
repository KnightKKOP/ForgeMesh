"""清理资源的部署：取消进行中的部署运行并停止实例（删除资源时、重新部署之前，以及释放
分配之前）。"""

from __future__ import annotations

import logging

from forgemesh.core.objects import Resource
from forgemesh.workflow.runtime import RunInfo, WorkflowRuntime

from .launcher import RuntimeLauncher
from .workflow import find_deployment_runs, launched_instance

logger = logging.getLogger(__name__)


class DeploymentReleaser:
    """协调器的 :class:`~forgemesh.controlplane.reconciler.ReleaseProvider`：资源进入
    ``Terminating`` 后清理它的部署，协调器随后释放分配并删除资源；部署新的 spec 之前，以及
    释放 ``Failed`` 服务的分配之前，协调器同样先经它停止旧的实例。

    按 :func:`~forgemesh.controlplane.deployment.workflow.find_deployment_runs` 找到资源（uid）
    的全部部署运行（每一代 spec 一次运行，实例丢失后重新部署时再启动一次，含 1.1.0 之前
    不含 generation 的运行），按创建先后逐个处理：

    * 运行未结束（正在部署）：``cancel(cleanup=True)`` 中断正在执行的步骤、补偿已完成的
      步骤（停止已启动的实例），并等待运行结束；
    * 之后实例仍可能在运行（服务已 ``Ready``，或补偿失败）：调用 ``launcher.stop``。

    没有部署运行（资源没有进入过 ``Preparing``）时什么也不做。任何一步失败都抛出异常，
    协调器保持当前 phase 并稍后重试；取消与停止都是幂等的。

    Args:
        runtime: 执行部署工作流的运行时（须实现 ``find_runs``，``get_run`` 须返回步骤输出）。
        launcher: 部署所用的启动器。
    """

    def __init__(self, runtime: WorkflowRuntime, launcher: RuntimeLauncher) -> None:
        self._runtime = runtime
        self._launcher = launcher

    async def release(self, resource: Resource) -> None:
        """取消 ``resource`` 的全部部署运行并停止它们的实例；失败时抛出异常。"""
        meta = resource.metadata
        if not meta.uid:
            return
        for deployment in await find_deployment_runs(self._runtime, meta.uid):
            await self._release_run(resource, deployment.run)

    async def _release_run(self, resource: Resource, run: RunInfo) -> None:
        if not run.terminal:
            await self._runtime.cancel(run.run_id, cleanup=True)
            run = await self._runtime.get_run(run.run_id)
        instance = launched_instance(run)
        if instance is None:
            return
        running = await self._launcher.is_running(instance.instance_id)
        await self._launcher.stop(instance.instance_id)
        if not running:
            return
        meta = resource.metadata
        logger.info(
            "instance stopped: instance_id=%s kind=%s namespace=%s name=%s uid=%s run_id=%s",
            instance.instance_id,
            resource.kind,
            meta.namespace,
            meta.name,
            meta.uid,
            run.run_id,
        )


__all__ = ["DeploymentReleaser"]
