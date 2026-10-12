"""推理实例启动器契约：部署工作流通过它准备、启动、验证和停止推理实例。

:class:`RuntimeLauncher` 是控制平面与运行时后端（本地进程、容器等）之间的边界。部署
工作流（:mod:`forgemesh.controlplane.deployment.workflow`）的三个步骤依次调用
:meth:`~RuntimeLauncher.prepare`、:meth:`~RuntimeLauncher.launch` 与
:meth:`~RuntimeLauncher.verify`；部署失败时的补偿与删除资源时调用
:meth:`~RuntimeLauncher.stop`。

契约放在控制平面而不是契约包中：启动器由控制平面按配置选择和组装，请求直接使用强类型
的 :class:`~forgemesh.resource.inference.InferenceServiceSpec`。插件不能导入控制平面，
以后若由插件提供启动器，需要先把契约移到契约包中。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import Endpoint, EndpointProtocol, InferenceServiceSpec
from pydantic import BaseModel, ConfigDict, Field, model_validator


class LaunchRequest(BaseModel):
    """启动一个推理实例所需的信息（部署工作流的输入，可 JSON 序列化）。

    Attributes:
        instance_key: 实例的幂等键，同一个资源（uid）的部署不变。
        tenant_id: 资源所属租户。
        resource: 被部署的资源（kind / namespace / name / uid）。
        spec: 资源的期望状态（模型、运行时、加速器申请、副本数）。
        allocation_id: 调度器为该资源记录的分配。
        devices: 分配给该资源的加速器设备（同一节点）；没有加速器申请时为空。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_key: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    resource: ResourceRef
    spec: InferenceServiceSpec
    allocation_id: str = Field(min_length=1)
    devices: tuple[AllocatedDevice, ...] = ()


class LaunchedInstance(BaseModel):
    """已启动的实例：控制器据此写入 ``status.endpoints``。

    Attributes:
        instance_id: 启动器内的实例标识，:meth:`RuntimeLauncher.stop` 据此停止实例。
        url: 实例基地址，规则同 :class:`~forgemesh.resource.inference.Endpoint` 的 ``url``
            （不带凭证、查询串与片段的 ``http`` / ``https`` URL）。
        protocol: 实例提供的协议，决定网关把请求转发到实例的哪个路径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(min_length=1)
    url: str
    protocol: EndpointProtocol

    @model_validator(mode="after")
    def _require_valid_endpoint(self) -> Self:
        self.endpoint()
        return self

    def endpoint(self) -> Endpoint:
        """``status.endpoints`` 中对应的一项（实例已就绪）。"""
        return Endpoint(
            url=self.url, ready=True, instanceId=self.instance_id, protocol=self.protocol
        )


class RuntimeLauncher(ABC):
    """推理实例启动器（运行时后端）。

    部署工作流的每一步可能重试（按工作流运行时的重试策略），也可能在控制平面重启后以
    相同的请求再执行一次，因此每个方法都须幂等；每次调用受该步骤的时限约束，超时或取消
    部署时调用方的任务被取消（收到 :class:`asyncio.CancelledError`）。
    """

    async def prepare(self, request: LaunchRequest) -> None:  # noqa: B027 - 可选步骤，默认不做事
        """准备实例需要的模型制品（下载、校验摘要等）；默认什么都不做。

        Raises:
            Exception: 无法准备；步骤按重试策略重试，用尽后部署失败。
        """

    @abstractmethod
    async def launch(self, request: LaunchRequest) -> LaunchedInstance:
        """启动实例并返回它的地址；实例开始启动即可返回，就绪由 :meth:`verify` 检查。

        同一 ``request.instance_key`` 再次调用时（重试、重启后重新执行该步骤），实例仍在
        运行就返回同一实例，不得启动第二个。抛出异常或被取消时须停止已经启动的部分：
        这种情况下调用方没有拿到实例 id，无法再停止它。

        Raises:
            Exception: 无法启动；步骤按重试策略重试，用尽后部署失败。
        """

    @abstractmethod
    async def verify(self, instance: LaunchedInstance) -> None:
        """检查实例能否提供服务（如请求 ``/health``），不能时抛出异常。

        失败按重试策略重试。实例需要较长时间加载模型时，实现应在一次调用内等待它就绪
        （直到被取消），而不是立即失败。

        Raises:
            Exception: 实例不能提供服务。
        """

    @abstractmethod
    async def stop(self, instance_id: str) -> None:
        """停止实例并释放它占用的资源；实例不存在或已停止时直接返回。

        部署失败的补偿和删除资源都会调用它，阻塞会让删除一直停在 ``Terminating``：须在
        有限时间内返回（例如先请求退出，超过宽限期后强制结束）。

        Raises:
            Exception: 无法确认实例已停止；调用方稍后重试。
        """


__all__ = ["LaunchRequest", "LaunchedInstance", "RuntimeLauncher"]
