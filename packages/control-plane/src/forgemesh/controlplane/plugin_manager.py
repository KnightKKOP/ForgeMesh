"""ForgeMesh 插件管理器。

对齐 development-plan 8.3 / ADR-006。负责插件注册、生命周期管理、
能力发现与权限校验。

设计约束：

* **安装状态与健康状态分离**（PluginLifecycleState vs 运行健康）：
  生命周期状态由治理状态机驱动（Discovered→Validating→Installed→
  Enabled/Disabled→Uninstalled）；运行健康（Healthy/Degraded/Unhealthy）
  独立存储于 ``health_status``，二者不得复用同一字段。
* **最小权限**：``permissions`` 声明插件所需的最小权限集合，安装时校验、
  运行时 ``check_permissions`` 按 ``required ⊆ granted`` 判定。
* **版本固定与并存规则**（ADR-011）：plugin_id 由 ``metadata.id`` 与
  ``metadata.version`` 组合，不同版本可并存。

生命周期转移表直接复用契约 :data:`forgemesh.plugin.manifest.PLUGIN_TRANSITIONS`，
非法转移抛 :class:`forgemesh.core.errors.InvalidStateTransition`。

兼容 Python 3.9+。
"""

from __future__ import annotations

from datetime import UTC, datetime

from forgemesh.core.errors import ConflictError, InvalidStateTransition, NotFoundError
from forgemesh.plugin.manifest import (
    PLUGIN_TRANSITIONS,
    PluginCategory,
    PluginLifecycleState,
    PluginManifest,
)
from pydantic import BaseModel, ConfigDict


class PluginRecord(BaseModel):
    """插件注册记录（内部模型）。

    Attributes:
        plugin_id: 插件唯一标识（``metadata.id:metadata.version``）。
        manifest: 原始插件清单。
        lifecycle_state: 治理生命周期状态，注册后默认 ``Discovered``。
        health_status: 运行健康状态（Healthy/Degraded/Unhealthy/Unknown），
            与 ``lifecycle_state`` 独立存储。
        installed_at: 进入 ``Installed`` 状态的 UTC ISO-8601 时间戳。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    manifest: PluginManifest
    lifecycle_state: PluginLifecycleState = PluginLifecycleState.DISCOVERED
    health_status: str = "Unknown"
    installed_at: str | None = None


class PluginManager:
    """插件管理器：注册、生命周期、能力发现、权限校验。

    线程安全性由调用方保证；本类为单进程内存注册表。
    """

    def __init__(self) -> None:
        self._plugins: dict[str, PluginRecord] = {}

    # ------------------------------------------------------------------ #
    # 注册与发现
    # ------------------------------------------------------------------ #

    def register(self, manifest: PluginManifest) -> str:
        """注册插件 Manifest，返回 plugin_id。

        plugin_id 由 ``metadata.id`` 与 ``metadata.version`` 组合，
        支持同一插件多版本并存（ADR-011）。重复注册同一 plugin_id 抛
        :class:`ConflictError`。

        Args:
            manifest: 插件清单。

        Returns:
            plugin_id 字符串。
        """
        plugin_id = f"{manifest.metadata.id}:{manifest.metadata.version}"
        if plugin_id in self._plugins:
            raise ConflictError(f"plugin already registered: {plugin_id}")
        record = PluginRecord(plugin_id=plugin_id, manifest=manifest)
        self._plugins[plugin_id] = record
        return plugin_id

    def get(self, plugin_id: str) -> PluginRecord:
        """获取插件注册记录。

        Raises:
            NotFoundError: 插件未注册。
        """
        if plugin_id not in self._plugins:
            raise NotFoundError(f"plugin not found: {plugin_id}")
        return self._plugins[plugin_id]

    def discover(
        self, category: PluginCategory | None = None
    ) -> list[PluginManifest]:
        """发现已注册插件，可按类别过滤。

        Args:
            category: 插件类别；``None`` 表示返回全部。

        Returns:
            匹配插件的 Manifest 列表。
        """
        results: list[PluginManifest] = []
        for record in self._plugins.values():
            if category is None or record.manifest.spec.category == category:
                results.append(record.manifest)
        return results

    # ------------------------------------------------------------------ #
    # 生命周期管理
    # ------------------------------------------------------------------ #

    def transition(
        self, plugin_id: str, target: PluginLifecycleState
    ) -> PluginRecord:
        """执行生命周期状态转移。

        合法转移由 :data:`forgemesh.plugin.manifest.PLUGIN_TRANSITIONS` 定义。
        非法转移抛 :class:`InvalidStateTransition`。

        进入 ``Installed`` 状态时记录 ``installed_at`` 时间戳。

        Args:
            plugin_id: 插件标识。
            target: 目标生命周期状态。

        Returns:
            更新后的插件记录。

        Raises:
            NotFoundError: 插件未注册。
            InvalidStateTransition: 转移不在合法集合内。
        """
        record = self.get(plugin_id)
        current = record.lifecycle_state
        allowed = PLUGIN_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidStateTransition(
                f"invalid lifecycle transition: {current.value} -> {target.value}"
            )
        record.lifecycle_state = target
        if target == PluginLifecycleState.INSTALLED:
            record.installed_at = datetime.now(UTC).isoformat()
        return record

    # ------------------------------------------------------------------ #
    # 能力发现
    # ------------------------------------------------------------------ #

    def get_capabilities(self, plugin_id: str) -> set[str]:
        """获取插件声明的能力集合（``spec.provides``）。

        Raises:
            NotFoundError: 插件未注册。
        """
        record = self.get(plugin_id)
        return set(record.manifest.spec.provides)

    # ------------------------------------------------------------------ #
    # 健康状态（与生命周期独立）
    # ------------------------------------------------------------------ #

    def set_health(self, plugin_id: str, status: str) -> None:
        """设置运行健康状态。

        健康状态（Healthy/Degraded/Unhealthy/Unknown）与生命周期状态
        独立存储，互不影响。

        Raises:
            NotFoundError: 插件未注册。
        """
        record = self.get(plugin_id)
        record.health_status = status

    def get_health(self, plugin_id: str) -> str:
        """获取运行健康状态。

        Raises:
            NotFoundError: 插件未注册。
        """
        return self.get(plugin_id).health_status

    # ------------------------------------------------------------------ #
    # 权限校验（最小权限原则）
    # ------------------------------------------------------------------ #

    def check_permissions(self, plugin_id: str, required: set[str]) -> bool:
        """校验插件是否拥有所需权限。

        按 ``required ⊆ granted`` 判定，其中 ``granted`` 取自
        ``spec.permissions``。空 ``required`` 恒通过。

        Args:
            plugin_id: 插件标识。
            required: 运行时所需权限集合。

        Returns:
            ``True`` 表示权限充足。
        """
        record = self.get(plugin_id)
        granted = set(record.manifest.spec.permissions)
        return required.issubset(granted)

    # ------------------------------------------------------------------ #
    # 版本管理
    # ------------------------------------------------------------------ #

    def get_version(self, plugin_id: str) -> str:
        """获取插件版本（``metadata.version``）。

        Raises:
            NotFoundError: 插件未注册。
        """
        return self.get(plugin_id).manifest.metadata.version

    def is_compatible(self, plugin_id: str, api_version: str) -> bool:
        """检查插件与 API 版本兼容性。

        优先检查 ``spec.compatibility["coreApi"]``；未声明时回退到
        ``manifest.apiVersion`` 精确匹配。

        Args:
            plugin_id: 插件标识。
            api_version: 期望的 API 版本（如 ``v1alpha1`` 或
                ``forgemesh.io/v1alpha1``）。

        Returns:
            ``True`` 表示兼容。
        """
        record = self.get(plugin_id)
        core_api = record.manifest.spec.compatibility.get("coreApi")
        if core_api is not None:
            return core_api == api_version
        return record.manifest.apiVersion == api_version

    # ------------------------------------------------------------------ #
    # 查询辅助
    # ------------------------------------------------------------------ #

    def list_installed(self) -> list[PluginRecord]:
        """列出所有已安装（Installed/Enabled/Disabled）的插件记录。"""
        installed_states = {
            PluginLifecycleState.INSTALLED,
            PluginLifecycleState.ENABLED,
            PluginLifecycleState.DISABLED,
        }
        return [
            r for r in self._plugins.values() if r.lifecycle_state in installed_states
        ]


__all__ = ["PluginManager", "PluginRecord"]
