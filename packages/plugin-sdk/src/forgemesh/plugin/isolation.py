"""插件隔离宿主与升级兼容（对齐 development-plan 5.7 / ADR-006 / IVT-8）。

设计要点：

* **信任分级**：``TRUSTED`` > ``VERIFIED`` > ``UNTRUSTED``，由安装器裁决并映射到
  隔离模式。
* **隔离模式**：

  - ``IN_PROCESS``——进程内执行，允许所有资源访问。
  - ``RESTRICTED_RPC``——受限 RPC，禁止直连 ``database`` / ``filesystem`` /
    ``network``，无直连 DB。

* **权限校验**：``UNTRUSTED`` 插件不得声明 ``database`` / ``admin`` 权限。
* **升级兼容（IVT-8）**：语义版本兼容检查；升级不静默改变既有工作流实例的
  执行定义——运行实例始终按其记录的插件版本执行，新版本仅作用于后续新建实例。

``IsolationError`` 复用 :class:`forgemesh.core.errors.ForgeMeshError` 统一异常体系；
若核心包未就绪，则回退到本地兜底基类。
"""

from __future__ import annotations

import re
from enum import IntEnum

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


# 受限 RPC 下禁止直接访问的资源。
_RESTRICTED_RESOURCES: frozenset[str] = frozenset({"database", "filesystem", "network"})
# UNTRUSTED 插件禁止声明的权限。
_UNTRUSTED_FORBIDDEN_PERMISSIONS: frozenset[str] = frozenset({"database", "admin"})


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

    def check_access(self, trust_level: TrustLevel, resource: str) -> None:
        """检查插件对指定资源的访问权限。

        Args:
            trust_level: 插件信任级别。
            resource: 资源标识（如 ``database`` / ``filesystem`` / ``network``）。

        Raises:
            IsolationError: ``RESTRICTED_RPC`` 模式下访问受限资源。
        """
        mode = self.get_mode(trust_level)
        if mode == IsolationMode.IN_PROCESS:
            return  # 进程内执行：允许所有资源
        # RESTRICTED_RPC：禁止直连 database/filesystem/network
        if resource in _RESTRICTED_RESOURCES:
            raise IsolationError(
                f"resource {resource!r} not accessible in RESTRICTED_RPC mode",
            )

    def validate_manifest(self, trust_level: TrustLevel, permissions: list[str]) -> None:
        """校验插件权限声明是否与其信任级别相符。

        Args:
            trust_level: 插件信任级别。
            permissions: 插件声明的权限列表。

        Raises:
            IsolationError: ``UNTRUSTED`` 插件声明了 ``database`` 或 ``admin`` 权限。
        """
        if trust_level == TrustLevel.UNTRUSTED:
            forbidden = _UNTRUSTED_FORBIDDEN_PERMISSIONS & set(permissions)
            if forbidden:
                raise IsolationError(
                    f"UNTRUSTED plugin cannot declare permissions: {sorted(forbidden)}",
                )


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
            return UpgradeCheck(
                compatible=True, breaking=False, reason=f"same version {current}"
            )
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
