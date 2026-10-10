"""ForgeMesh 插件 SDK 契约（命名空间包 ``forgemesh.plugin``）。

本包定义阶段 0 的插件契约（对齐 ``development-plan`` 5.7 / 6.3）：

* :class:`PluginManifest`        —— 声明式插件清单（类别/兼容性/能力/权限/隔离）。
* :class:`PluginLifecycleState`  —— 插件生命周期状态机。

核心原则：**安装状态与健康状态分离**。``Installed/Enabled`` 描述的是治理
生命周期，不代表运行健康；运行健康由独立健康检查（Healthy/Degraded/Unhealthy）
表达，二者不得混用同一字段。
"""

from __future__ import annotations

from .manifest import (
    PLUGIN_TRANSITIONS,
    PluginCategory,
    PluginLifecycleState,
    PluginManifest,
    PluginMeta,
    PluginSpec,
    validate_plugin_transition,
)

__all__ = [
    "PluginCategory",
    "PluginMeta",
    "PluginSpec",
    "PluginManifest",
    "PluginLifecycleState",
    "PLUGIN_TRANSITIONS",
    "validate_plugin_transition",
]
