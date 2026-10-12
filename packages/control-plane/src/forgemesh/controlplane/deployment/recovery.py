"""控制平面重启之后核对已部署的实例，不再对外宣告没能活过重启的实例。

实例随控制平面进程结束的启动器（本进程内的假实例、子进程）在重启之后不认识重启之前的
实例，``Ready`` 服务的 ``status.endpoints`` 却仍指向它们，网关会继续转发过去。应用在
启动控制器之前调用 :func:`mark_lost_instances`：实例已不在运行的服务转为 ``Degraded``，
这些实例的 endpoint 标记为未就绪，``Ready=False``（``InstanceLost``），网关不再向它们
转发（网关缓存解析结果的几秒内除外）。

不会自动重新部署：``Degraded`` 的服务保留分配，直到被删除；删除后重新创建即可再次部署。
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


async def mark_lost_instances(
    registry: SQLiteRegistry, launcher: RuntimeLauncher
) -> list[ResourceKey]:
    """把实例已不在运行的 ``Ready`` 服务转为 ``Degraded``，返回被转换的服务。

    只检查 ``phase=Ready`` 的 ``InferenceService``：``status.endpoints`` 中启动器不在运行的
    实例（没有 ``instanceId`` 的 endpoint 无从确认，同样算作不在运行）标记为
    ``ready: false``，``phase`` 转为 ``Degraded``，``Ready=False``（``InstanceLost``）。
    写入以读到的版本做 CAS，资源在此期间被修改或删除时跳过它。须在控制器启动之前调用。
    """
    marked: list[ResourceKey] = []
    for key in await registry.list_resource_keys([_KIND]):
        try:
            resource, phase = await registry.get_with_phase(
                key.kind, key.namespace, key.name, key.tenant_id
            )
        except NotFoundError:
            continue
        if phase != InferencePhase.READY or not isinstance(resource, InferenceService):
            continue
        endpoints = resource.status.endpoints
        lost = [
            endpoint.instanceId is None or not await launcher.is_running(endpoint.instanceId)
            for endpoint in endpoints
        ]
        if not any(lost):
            continue
        described = " and ".join(
            _describe(endpoint) for endpoint, gone in zip(endpoints, lost, strict=True) if gone
        )
        message = (
            f"{described} did not survive the control plane restart; delete and re-create "
            "the service to deploy it again"
        )
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
                    "message": message,
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
            described,
            key.kind,
            key.namespace,
            key.name,
            meta.uid,
            key.tenant_id,
        )
        marked.append(key)
    return marked


__all__ = ["INSTANCE_LOST_REASON", "mark_lost_instances"]
