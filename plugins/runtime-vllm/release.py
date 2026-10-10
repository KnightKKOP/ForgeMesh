"""资源回收：停止实例、释放租约、分配核验。

对齐 ``docs/architecture/development-plan.md`` 5.3。在推理服务生命周期结束时，
归还占用的计算资源：

* 停止 vLLM 实例（通过 :class:`VLLMDeployer`）；
* 释放资源租约（设备/GPU 归还资源池）；
* 分配核验（确认设备实际已释放，不虚占）。

架构不变量：

* **资源归还**：``release`` = 停止实例 + 释放租约 + 分配核验，三者均成功才
  ``released=True``；``verified`` 单独反映分配核验结果。
* **不虚占**：``verify_allocation`` 检查设备实际状态，而非仅信任租约释放返回值。
* **测试友好**：租约释放与分配核验函数均可注入 mock。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .deploy import VLLMDeployer

#: 租约释放函数类型：给定 lease_id，返回是否释放成功。
LeaseReleaser = Callable[[str], Awaitable[bool]]

#: 分配核验函数类型：给定设备 id 列表，返回设备是否均已释放（不虚占）。
AllocationVerifier = Callable[[list[int]], Awaitable[bool]]


class ReleaseResult(BaseModel):
    """资源回收结果。

    Attributes:
        instance_id: 实例 id。
        released: 实例与租约是否均已成功回收。
        verified: 分配核验是否通过（设备实际已释放）。
        details: 人类可读详情。
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    released: bool = False
    verified: bool = False
    details: str = ""


async def _default_release_lease(lease_id: str) -> bool:
    """默认租约释放：实际环境应对接资源调度器。

    未注入 releaser 时抛 ``NotImplementedError``，提示调用方注入 mock 或
    配置真实释放实现。
    """
    raise NotImplementedError(
        "default lease releaser is not configured; inject a releaser for production use",
    )


async def _default_verify_allocation(device_ids: list[int]) -> bool:
    """默认分配核验：实际环境应查询设备实际占用状态。

    未注入 verifier 时抛 ``NotImplementedError``，提示调用方注入 mock 或
    配置真实核验实现。
    """
    raise NotImplementedError(
        "default allocation verifier is not configured; inject a verifier for production use",
    )


class ResourceReleaser:
    """资源回收器：停止实例、释放租约、分配核验。

    Args:
        lease_releaser: 租约释放函数（可注入 mock）。
        allocation_verifier: 分配核验函数（可注入 mock）。
    """

    def __init__(
        self,
        *,
        lease_releaser: LeaseReleaser | None = None,
        allocation_verifier: AllocationVerifier | None = None,
    ) -> None:
        self._release_lease_fn: LeaseReleaser = (
            lease_releaser or _default_release_lease
        )
        self._verify_allocation_fn: AllocationVerifier = (
            allocation_verifier or _default_verify_allocation
        )

    async def stop_instance(
        self,
        deployer: VLLMDeployer,
        instance_id: str,
    ) -> ReleaseResult:
        """停止实例。

        Args:
            deployer: 部署器，用于停止实例。
            instance_id: 实例 id。

        Returns:
            :class:`ReleaseResult`，``released`` 反映实例是否成功停止。
        """
        stopped = await deployer.stop_instance(instance_id)
        if stopped is None:
            return ReleaseResult(
                instance_id=instance_id,
                released=False,
                verified=False,
                details=f"instance {instance_id} not found",
            )
        return ReleaseResult(
            instance_id=instance_id,
            released=True,
            verified=False,
            details=f"instance {instance_id} stopped",
        )

    async def release_lease(self, lease_id: str) -> bool:
        """释放资源租约。

        Args:
            lease_id: 租约 id。

        Returns:
            ``True`` 表示租约释放成功。
        """
        return await self._release_lease_fn(lease_id)

    async def verify_allocation(self, device_ids: list[int]) -> bool:
        """分配核验：确认设备实际已释放，不虚占。

        Args:
            device_ids: 设备 id 列表。

        Returns:
            ``True`` 表示所有设备均已实际释放。
        """
        return await self._verify_allocation_fn(device_ids)

    async def release(
        self,
        deployer: VLLMDeployer,
        instance_id: str,
        lease_id: str,
        device_ids: list[int],
    ) -> ReleaseResult:
        """完整回收流程：停止实例 + 释放租约 + 分配核验。

        Args:
            deployer: 部署器。
            instance_id: 实例 id。
            lease_id: 资源租约 id。
            device_ids: 占用的设备 id 列表。

        Returns:
            :class:`ReleaseResult`：

            * ``released``：实例停止且租约释放均成功。
            * ``verified``：分配核验通过（设备实际已释放）。
            * ``details``：汇总各步骤结果。
        """
        steps: dict[str, Any] = {}

        # 1. 停止实例。
        stop_result = await self.stop_instance(deployer, instance_id)
        steps["instance_stopped"] = stop_result.released
        if not stop_result.released:
            return ReleaseResult(
                instance_id=instance_id,
                released=False,
                verified=False,
                details=f"stop instance failed: {stop_result.details}",
            )

        # 2. 释放租约。
        try:
            lease_ok = await self.release_lease(lease_id)
        except Exception as exc:
            lease_ok = False
            steps["lease_release_error"] = str(exc)
        steps["lease_released"] = lease_ok

        # 3. 分配核验。
        try:
            alloc_ok = await self.verify_allocation(device_ids)
        except Exception as exc:
            alloc_ok = False
            steps["allocation_verify_error"] = str(exc)
        steps["allocation_verified"] = alloc_ok

        released = stop_result.released and lease_ok
        detail_parts = [
            f"instance_stopped={stop_result.released}",
            f"lease_released={lease_ok}",
            f"allocation_verified={alloc_ok}",
        ]
        return ReleaseResult(
            instance_id=instance_id,
            released=released,
            verified=alloc_ok,
            details="; ".join(detail_parts),
        )


__all__ = [
    "AllocationVerifier",
    "LeaseReleaser",
    "ReleaseResult",
    "ResourceReleaser",
]
