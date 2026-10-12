"""删除资源时清理它的部署：取消进行中的部署运行并停止实例。"""

from __future__ import annotations

import logging

from forgemesh.core.objects import Resource
from forgemesh.workflow.runtime import WorkflowRuntime

from .launcher import RuntimeLauncher
from .workflow import deployment_run_key, launched_instance

logger = logging.getLogger(__name__)


class DeploymentReleaser:
    """协调器的 :class:`~forgemesh.controlplane.reconciler.ReleaseProvider`：资源进入
    ``Terminating`` 后清理它的部署，协调器随后释放分配并删除资源。

    按 :func:`~forgemesh.controlplane.deployment.workflow.deployment_run_key` 找到资源（uid）
    的部署运行：

    * 运行未结束（正在部署）：``cancel(cleanup=True)`` 中断正在执行的步骤、补偿已完成的
      步骤（停止已启动的实例），并等待运行结束；
    * 之后实例仍可能在运行（服务已 ``Ready``，或补偿失败）：调用 ``launcher.stop``。

    没有部署运行（资源没有进入过 ``Preparing``）时什么也不做。任何一步失败都抛出异常，
    协调器保持 ``Terminating`` 并稍后重试；取消与停止都是幂等的。

    Args:
        runtime: 执行部署工作流的运行时（须实现 ``find_run``，``get_run`` 须返回步骤输出）。
        launcher: 部署所用的启动器。
    """

    def __init__(self, runtime: WorkflowRuntime, launcher: RuntimeLauncher) -> None:
        self._runtime = runtime
        self._launcher = launcher

    async def release(self, resource: Resource) -> None:
        """取消 ``resource`` 的部署并停止它的实例；失败时抛出异常。"""
        meta = resource.metadata
        if not meta.uid:
            return
        run = await self._runtime.find_run(deployment_run_key(meta.uid))
        if run is None:
            return
        if not run.terminal:
            await self._runtime.cancel(run.run_id, cleanup=True)
            run = await self._runtime.get_run(run.run_id)
        instance = launched_instance(run)
        if instance is None:
            return
        await self._launcher.stop(instance.instance_id)
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
