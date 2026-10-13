"""核对已部署的实例：不再对外宣告已不在运行的实例，交给协调器重新部署。

实例随控制平面进程结束的启动器（本进程内的假实例、子进程）在重启之后不认识重启之前的
实例；实例也可能在运行中退出。``Ready`` 服务的 ``status.endpoints`` 却仍指向它们，网关会
继续转发过去。:func:`check_instances` 经启动器核对实例：实例已不在运行的服务转为
``Degraded``，这些实例的 endpoint 标记为未就绪，``Ready=False``（``InstanceLost``），网关
不再向它们转发（网关缓存解析结果的几秒内除外）；协调器随后按退避重新部署这些服务（见
:mod:`forgemesh.controlplane.reconciler.core`）。应用在启动控制器之前调用一次。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.resource.inference import Endpoint, InferencePhase, InferenceService

from .launcher import RuntimeLauncher

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用
    from ..registry import ResourceKey, SQLiteRegistry

logger = logging.getLogger(__name__)

#: 实例已不在运行时 ``Ready`` 条件的原因。
INSTANCE_LOST_REASON = "InstanceLost"

_KIND = "InferenceService"


def _describe(endpoint: Endpoint) -> str:
    if endpoint.instanceId is None:
        return f"the instance at {endpoint.url}"
    return f"instance {endpoint.instanceId} at {endpoint.url}"


async def _running(launcher: RuntimeLauncher, endpoint: Endpoint) -> bool:
    return endpoint.instanceId is not None and await launcher.is_running(endpoint.instanceId)


async def check_instances(
    registry: SQLiteRegistry, launcher: RuntimeLauncher, *, after_restart: bool = False
) -> list[ResourceKey]:
    """核对 ``Ready`` / ``Degraded`` 服务的实例，返回要交给协调器处理的服务。

    * ``Ready`` 的服务：``status.endpoints`` 中启动器不在运行的实例（没有 ``instanceId`` 的
      endpoint 无从确认，同样算作不在运行）标记为 ``ready: false``，``phase`` 转为
      ``Degraded``，``Ready=False``（``InstanceLost``，``after_restart`` 为真时消息说明实例
      没能活过控制平面重启）。
    * ``Degraded`` 的服务：endpoint 不为空且实例全部在运行（实例回来了）时只返回它，由
      协调器转回 ``Ready``。

    写入以读到的版本做 CAS，资源在此期间被修改或删除时跳过它；不修改
    ``observedGeneration``。
    """
    found: list[ResourceKey] = []
    for key in await registry.list_resource_keys([_KIND]):
        try:
            resource, phase = await registry.get_with_phase(
                key.kind, key.namespace, key.name, key.tenant_id
            )
        except NotFoundError:
            continue
        if not isinstance(resource, InferenceService):
            continue
        endpoints = resource.status.endpoints
        if phase == InferencePhase.DEGRADED:
            if endpoints and all([await _running(launcher, endpoint) for endpoint in endpoints]):
                found.append(key)
            continue
        if phase != InferencePhase.READY:
            continue
        lost = [not await _running(launcher, endpoint) for endpoint in endpoints]
        if not any(lost):
            continue
        described = " and ".join(
            _describe(endpoint) for endpoint, gone in zip(endpoints, lost, strict=True) if gone
        )
        if after_restart:
            message = f"{described} did not survive the control plane restart"
        else:
            message = f"{described} is not running any more"
        patch = {
            "phase": InferencePhase.DEGRADED.value,
            "endpoints": [
                endpoint.model_copy(update={"ready": endpoint.ready and not gone}).model_dump(
                    mode="json"
                )
                for endpoint, gone in zip(endpoints, lost, strict=True)
            ],
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": INSTANCE_LOST_REASON,
                    "message": f"{message}; redeploying the service",
                }
            ],
        }
        meta = resource.metadata
        try:
            await registry.update_status(
                key.kind,
                key.namespace,
                key.name,
                key.tenant_id,
                patch,
                expected_version=meta.resourceVersion,
            )
        except (ConflictError, NotFoundError) as exc:
            logger.warning(
                "lost instance not reported, the service changed meanwhile: namespace=%s "
                "name=%s tenant_id=%s error=%s",
                key.namespace,
                key.name,
                key.tenant_id,
                exc,
            )
            continue
        logger.warning(
            "service degraded, its instance is gone: %s; kind=%s namespace=%s name=%s uid=%s "
            "tenant_id=%s",
            message,
            key.kind,
            key.namespace,
            key.name,
            meta.uid,
            key.tenant_id,
        )
        found.append(key)
    return found


__all__ = ["INSTANCE_LOST_REASON", "check_instances"]
