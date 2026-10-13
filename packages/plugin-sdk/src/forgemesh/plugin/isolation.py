"""插件隔离宿主与升级兼容（对齐 development-plan 5.7 / ADR-006 / IVT-8）。

设计要点：

* **信任分级**：``TRUSTED`` > ``VERIFIED`` > ``UNTRUSTED``，由安装器裁决并映射到
  隔离模式。
* **隔离模式**：

  - ``IN_PROCESS``——进程内执行，允许全部权限。
  - ``RESTRICTED_RPC``——受限 RPC，不允许绕过宿主 API 直接访问宿主资源的权限
    （``network.egress`` / ``filesystem.access`` / ``database.access``）。

* **权限校验**：``UNTRUSTED`` 插件不得申请 ``database.access`` / ``platform.admin``。
* **升级兼容（IVT-8）**：语义版本兼容检查；升级不静默改变既有工作流实例的
  执行定义——运行实例始终按其记录的插件版本执行，新版本仅作用于后续新建实例。

权限名与插件清单共用 :class:`~forgemesh.plugin.permissions.Permission` 词表，未知的
权限名直接报错。本模块只回答「某个信任级别的插件能否使用某项权限」，不建立任何进程
边界：``RESTRICTED_RPC`` 尚无实现，插件代码实际能访问什么不受这里的检查约束。

``IsolationError`` 复用 :class:`forgemesh.core.errors.ForgeMeshError` 统一异常体系；
若核心包未就绪，则回退到本地兜底基类。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import IntEnum

from forgemesh.plugin.permissions import Permission
from pydantic import BaseModel

try:  # 优先复用核心包统一异常体系
    from forgemesh.core.errors import ForgeMeshError
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class ForgeMeshError(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的领域异常基类。"""

        def __init__(self, message: str = "", details: dict | None = None) -> None:
            super().__init__(message)
            self.message = message
            self.details = details


class IsolationError(ForgeMeshError):
    """隔离违规：插件试图访问未授权资源或声明越权权限。"""


class TrustLevel(IntEnum):
    """插件信任级别。

    值越小信任度越高。
    """

    TRUSTED = 0
    VERIFIED = 1
    UNTRUSTED = 2


class IsolationMode(IntEnum):
    """插件隔离模式。"""

    IN_PROCESS = 0
    RESTRICTED_RPC = 1


# 受限 RPC 下不可用的权限：它们绕过宿主 API，直接访问宿主资源。
_DIRECT_ACCESS_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.NETWORK_EGRESS, Permission.FILESYSTEM_ACCESS, Permission.DATABASE_ACCESS}
)
# UNTRUSTED 插件禁止申请的权限。
_UNTRUSTED_FORBIDDEN_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.DATABASE_ACCESS, Permission.PLATFORM_ADMIN}
)


def _to_permission(value: Permission | str) -> Permission:
    try:
        return Permission(value)
    except ValueError as exc:
        raise IsolationError(f"unknown permission {value!r}") from exc


class IsolationPolicy:
    """信任级别到隔离模式的映射策略。

    默认映射（ADR-006）：

    * ``TRUSTED``    → ``IN_PROCESS``（进程内执行）
    * ``VERIFIED``   → ``RESTRICTED_RPC``（受限 RPC）
    * ``UNTRUSTED``  → ``RESTRICTED_RPC``（受限 RPC）
    """

    def __init__(self, mapping: dict[TrustLevel, IsolationMode] | None = None) -> None:
        self.mapping: dict[TrustLevel, IsolationMode] = mapping or {
            TrustLevel.TRUSTED: IsolationMode.IN_PROCESS,
            TrustLevel.VERIFIED: IsolationMode.RESTRICTED_RPC,
            TrustLevel.UNTRUSTED: IsolationMode.RESTRICTED_RPC,
        }

    def resolve(self, trust_level: TrustLevel) -> IsolationMode:
        """解析信任级别对应的隔离模式。"""
        return self.mapping[trust_level]


class IsolationGuard:
    """隔离守卫：运行时资源访问校验与安装时权限声明校验。"""

    def __init__(self, policy: IsolationPolicy | None = None) -> None:
        self.policy = policy or IsolationPolicy()

    def get_mode(self, trust_level: TrustLevel) -> IsolationMode:
        """获取信任级别对应的隔离模式。"""
        return self.policy.resolve(trust_level)

    def check_access(self, trust_level: TrustLevel, permission: Permission | str) -> None:
        """检查指定信任级别的插件能否使用某项权限。

        Args:
            trust_level: 插件信任级别。
            permission: :class:`Permission` 词表中的权限名（如 ``network.egress``）。

        Raises:
            IsolationError: 权限名未知，或 ``RESTRICTED_RPC`` 模式下使用直接访问宿主资源
                的权限。
        """
        perm = _to_permission(permission)
        if self.get_mode(trust_level) == IsolationMode.IN_PROCESS:
            return
        if perm in _DIRECT_ACCESS_PERMISSIONS:
            raise IsolationError(f"permission {perm.value!r} not available in RESTRICTED_RPC mode")

    def validate_manifest(
        self, trust_level: TrustLevel, permissions: Iterable[Permission | str]
    ) -> None:
        """校验插件申请的权限是否与其信任级别相符。

        Args:
            trust_level: 插件信任级别。
            permissions: 插件清单申请的权限。

        Raises:
            IsolationError: 权限名未知，或 ``UNTRUSTED`` 插件申请了 ``database.access`` /
                ``platform.admin``。
        """
        requested = {_to_permission(p) for p in permissions}
        if trust_level == TrustLevel.UNTRUSTED:
            forbidden = sorted(p.value for p in requested & _UNTRUSTED_FORBIDDEN_PERMISSIONS)
            if forbidden:
                raise IsolationError(f"UNTRUSTED plugin cannot request permissions: {forbidden}")


# ---------------------------------------------------------------------------
# 版本兼容与升级（IVT-8）
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _parse_semver(version: str) -> tuple[int, int, int]:
    """解析语义版本为 ``(major, minor, patch)`` 元组。

    Args:
        version: 语义版本字符串（如 ``1.2.3``）。

    Returns:
        ``(major, minor, patch)`` 元组。

    Raises:
        ValueError: 版本格式不合法。
    """
    m = _SEMVER_RE.match(version)
    if m is None:
        raise ValueError(f"invalid semver: {version!r}")
    return (int(m[1]), int(m[2]), int(m[3]))


class UpgradeCheck(BaseModel):
    """升级检查结果。

    Attributes:
        compatible: 目标版本是否与当前版本兼容（同 major 且不降级）。
        breaking: 是否为破坏性升级（major 版本号递增）。
        reason: 人类可读的检查结论。
    """

    compatible: bool
    breaking: bool
    reason: str


class PluginVersionCompat:
    """插件语义版本兼容检查与升级评估。

    IVT-8 不变量：升级不静默改变既有工作流实例的执行定义。运行实例始终按其
    记录的插件版本执行；新版本仅作用于后续新建实例。由
    :meth:`instance_version_after_upgrade` 显式保证。
    """

    def is_compatible(self, plugin_version: str, required_version: str) -> bool:
        """检查插件版本是否与所需版本兼容。

        兼容规则：同 major 版本且 ``plugin_version`` 不低于 ``required_version``。

        Args:
            plugin_version: 插件实际版本。
            required_version: 所需最低版本。

        Returns:
            兼容返回 ``True``。
        """
        pv = _parse_semver(plugin_version)
        rv = _parse_semver(required_version)
        return pv[0] == rv[0] and pv >= rv

    def check_upgrade(self, current: str, target: str) -> UpgradeCheck:
        """评估从 ``current`` 升级到 ``target`` 的兼容性与破坏性。

        Args:
            current: 当前插件版本。
            target: 目标插件版本。

        Returns:
            :class:`UpgradeCheck` 检查结果。
        """
        cur = _parse_semver(current)
        tgt = _parse_semver(target)

        if tgt == cur:
            return UpgradeCheck(compatible=True, breaking=False, reason=f"same version {current}")
        if tgt < cur:
            return UpgradeCheck(
                compatible=False,
                breaking=True,
                reason=f"downgrade {current} -> {target} not allowed",
            )

        # 升级（target > current）
        breaking = tgt[0] > cur[0]
        compatible = tgt[0] == cur[0]
        if breaking:
            reason = (
                f"breaking upgrade {current} -> {target}; "
                f"existing instances pinned to {current} (IVT-8)"
            )
        else:
            reason = f"non-breaking upgrade {current} -> {target}"
        return UpgradeCheck(compatible=compatible, breaking=breaking, reason=reason)

    def instance_version_after_upgrade(self, recorded_version: str, target: str) -> str:
        """返回既有实例在升级后应使用的插件版本（IVT-8）。

        升级不静默改变既有工作流实例的执行定义：运行实例始终按其记录的插件
        版本执行，新版本仅作用于后续新建实例。

        Args:
            recorded_version: 实例创建时记录的插件版本。
            target: 升级目标版本（仅作用于后续新建实例）。

        Returns:
            既有实例应使用的版本，即 ``recorded_version``（不变）。
        """
        return recorded_version


__all__ = [
    "TrustLevel",
    "IsolationMode",
    "IsolationPolicy",
    "IsolationError",
    "IsolationGuard",
    "PluginVersionCompat",
    "UpgradeCheck",
]
