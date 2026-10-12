"""插件权限词表（对齐 development-plan 5.7 / ADR-006）。

插件清单的 ``spec.permissions``、管理员的权限授予记录（见
:mod:`forgemesh.controlplane.plugin_manager`）与隔离检查（:mod:`forgemesh.plugin.isolation`）
共用这一份词表；``api/schemas/plugin-manifest.schema.json`` 中的权限枚举与它一致。

权限名的形式为 ``<域>.<动作>``。清单中的权限只是插件的申请，插件实际拥有的权限是
申请与管理员授予的交集。
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """插件可以申请的权限。"""

    #: 经宿主 API 读取资源对象。
    RESOURCE_READ = "resource.read"
    #: 经宿主 API 写入资源对象。
    RESOURCE_WRITE = "resource.write"
    #: 作为决策 Provider 产出候选。
    DECISION_EVALUATE = "decision.evaluate"
    #: 参与策略（如 QoS 准入）评估。
    POLICY_EVALUATE = "policy.evaluate"
    #: 执行工作流。
    WORKFLOW_EXECUTE = "workflow.execute"
    #: 部署与释放推理实例。
    INFERENCE_DEPLOY = "inference.deploy"
    #: 查询推理实例状态。
    INFERENCE_INSPECT = "inference.inspect"
    #: 读取本机设备信息。
    DEVICE_READ = "device.read"
    #: 对设备分区（如 MIG）。
    DEVICE_PARTITION = "device.partition"
    #: 读取计量数据。
    METERING_READ = "metering.read"
    #: 订阅平台事件流。
    EVENT_SUBSCRIBE = "event.subscribe"
    #: 直接向外部发起网络连接。
    NETWORK_EGRESS = "network.egress"
    #: 直接读写宿主文件系统。
    FILESYSTEM_ACCESS = "filesystem.access"
    #: 直接连接控制平面数据库。
    DATABASE_ACCESS = "database.access"
    #: 平台管理权限。
    PLATFORM_ADMIN = "platform.admin"


__all__ = ["Permission"]
