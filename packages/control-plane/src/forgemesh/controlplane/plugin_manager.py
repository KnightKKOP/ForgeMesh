"""ForgeMesh 插件管理器。

对齐 development-plan 8.3 / ADR-006。负责插件注册校验、生命周期管理、
能力发现与权限校验。

设计约束：

* **安装状态与健康状态分离**（PluginLifecycleState vs 运行健康）：
  生命周期状态由治理状态机驱动（Discovered→Validating→Installed→
  Enabled/Disabled→Uninstalled）；运行健康（Healthy/Degraded/Unhealthy）
  独立存储于 ``health_status``，二者不得复用同一字段。
* **注册即校验**：:meth:`PluginManager.register` 按 :class:`PluginManifest` 重新校验清单，
  并用策略中受信任的公钥校验制品签名（:mod:`forgemesh.plugin.signing`）。签名覆盖制品内容
  与清单，篡改任何一方注册都会被拒绝。未签名的插件只能在显式开启的开发模式
  （``dev_mode_allow_unsigned=True``，默认关闭）下注册。
* **最小权限**：清单的 ``permissions`` 只是申请。插件实际拥有的权限是申请与管理员授予
  （:class:`PluginGrant`）的交集，``check_permissions`` 按 ``required ⊆ 实际权限`` 判定。
* **版本固定与并存规则**（ADR-011）：plugin_id 由 ``metadata.id`` 与
  ``metadata.version`` 组合，不同版本可并存。

插件策略文件（JSON，由管理员维护，见 :func:`load_plugin_policy`）::

    {"trustedKeys": ["<Ed25519 公钥 PEM>"],
     "grants": [{"pluginId": "forgemesh.decision.rules",
                 "permissions": ["resource.read", "decision.evaluate"]}]}

``trustedKeys`` 是插件作者的签名公钥，``python -m forgemesh.plugin.signtool keygen`` 会输出
可以直接追加的条目；``grants`` 按插件的 ``metadata.id`` 授权，对该插件的所有版本生效。

生命周期转移表直接复用契约 :data:`forgemesh.plugin.manifest.PLUGIN_TRANSITIONS`，
非法转移抛 :class:`forgemesh.core.errors.InvalidStateTransition`。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from forgemesh.core.errors import (
    ConflictError,
    ForbiddenError,
    InvalidStateTransition,
    NotFoundError,
)
from forgemesh.plugin.manifest import (
    PLUGIN_TRANSITIONS,
    PluginCategory,
    PluginLifecycleState,
    PluginManifest,
)
from forgemesh.plugin.permissions import Permission
from forgemesh.plugin.signing import PluginSignature, PluginSigner, SigningError, load_trusted_keys
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticSerializationError

logger = logging.getLogger(__name__)


class PluginPolicyError(ValueError):
    """插件策略配置不可读或非法；宿主应拒绝启动。"""


class PluginRejectedError(ForbiddenError):
    """插件未通过注册校验（清单非法、未签名或签名校验失败），注册被拒绝。"""


class PluginGrant(BaseModel):
    """管理员授予一个插件的权限（策略文件 ``grants`` 中的一条）。

    字段只按 JSON 名（``pluginId``）解析。``pluginId`` 是插件的 ``metadata.id``（不带版本），
    授权对该插件的所有版本生效。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str = Field(alias="pluginId", min_length=1)
    permissions: tuple[Permission, ...]

    @field_validator("plugin_id")
    @classmethod
    def _unversioned(cls, value: str) -> str:
        if ":" in value:
            raise ValueError(
                f"pluginId must be a plugin metadata.id without a version, got {value!r}"
            )
        return value

    @field_validator("permissions")
    @classmethod
    def _unique_permissions(cls, value: tuple[Permission, ...]) -> tuple[Permission, ...]:
        duplicates = sorted({p.value for p in value if value.count(p) > 1})
        if duplicates:
            raise ValueError(f"duplicate permissions: {duplicates}")
        return value


class PluginPolicy(BaseModel):
    """插件策略：受信任的签名公钥与管理员的权限授予（策略文件格式见模块文档）。

    Attributes:
        trusted_keys: 受信任的 Ed25519 公钥（PEM）；只有这些公钥签名的插件能够注册。
        grants: 权限授予，每个插件至多一条。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trusted_keys: tuple[str, ...] = Field(default=(), alias="trustedKeys")
    grants: tuple[PluginGrant, ...] = ()

    @field_validator("trusted_keys")
    @classmethod
    def _valid_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            load_trusted_keys(value)
        except SigningError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("grants")
    @classmethod
    def _one_grant_per_plugin(cls, value: tuple[PluginGrant, ...]) -> tuple[PluginGrant, ...]:
        ids = [grant.plugin_id for grant in value]
        duplicates = sorted({plugin_id for plugin_id in ids if ids.count(plugin_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate grants for plugins: {duplicates}")
        return value


def load_plugin_policy(path: str | os.PathLike[str]) -> PluginPolicy:
    """读取并校验插件策略文件。

    Raises:
        PluginPolicyError: 文件不可读，或内容不符合策略文件格式（包括非法或重复的公钥、
            重复的授予、词表之外的权限）。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginPolicyError(f"cannot read plugin policy file {path}: {exc}") from exc
    try:
        # model_validate_json 会静默丢弃与 Python 字段名同名的键（如 ``trusted_keys``），
        # 先解析为 Python 对象再校验，extra="forbid" 才会拒绝这类拼写错误。
        return PluginPolicy.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise PluginPolicyError(f"invalid plugin policy file {path}: {exc}") from exc


class PluginRecord(BaseModel):
    """插件注册记录（内部模型）。

    Attributes:
        plugin_id: 插件唯一标识（``metadata.id:metadata.version``）。
        manifest: 注册时校验过的插件清单（调用方所传清单的副本）。
        lifecycle_state: 治理生命周期状态，注册后默认 ``Discovered``。
        health_status: 运行健康状态（Healthy/Degraded/Unhealthy/Unknown），
            与 ``lifecycle_state`` 独立存储。
        installed_at: 进入 ``Installed`` 状态的 UTC ISO-8601 时间戳。
        signing_key_id: 签名公钥的 key id；开发模式下注册的未签名插件为 ``None``。
        artifact_digest: 注册时校验过的制品摘要（``sha256:<hex>``）；未签名插件为 ``None``。
    """

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    manifest: PluginManifest
    lifecycle_state: PluginLifecycleState = PluginLifecycleState.DISCOVERED
    health_status: str = "Unknown"
    installed_at: str | None = None
    signing_key_id: str | None = None
    artifact_digest: str | None = None


class PluginManager:
    """插件管理器：注册校验、生命周期、能力发现、权限校验。

    线程安全性由调用方保证；本类为单进程内存注册表。

    Args:
        policy: 受信任的签名公钥与权限授予；``None`` 表示两者都为空，此时没有任何签名
            被接受，也没有任何权限生效。
        dev_mode_allow_unsigned: 开发模式，允许注册未签名的插件，只用于本地开发，默认关闭。
            带签名的插件在开发模式下照常校验。
    """

    def __init__(
        self, policy: PluginPolicy | None = None, *, dev_mode_allow_unsigned: bool = False
    ) -> None:
        policy = policy if policy is not None else PluginPolicy()
        self._trusted_keys = load_trusted_keys(policy.trusted_keys)
        self._grants: dict[str, frozenset[Permission]] = {
            grant.plugin_id: frozenset(grant.permissions) for grant in policy.grants
        }
        self._dev_mode_allow_unsigned = dev_mode_allow_unsigned
        self._signer = PluginSigner()
        self._plugins: dict[str, PluginRecord] = {}
        if dev_mode_allow_unsigned:
            logger.warning(
                "plugin development mode is on: unsigned plugins are accepted at registration; "
                "use it for local development only"
            )

    @classmethod
    def from_policy_file(
        cls, path: str | os.PathLike[str], *, dev_mode_allow_unsigned: bool = False
    ) -> PluginManager:
        """按策略文件构造插件管理器（见 :func:`load_plugin_policy`）。"""
        return cls(load_plugin_policy(path), dev_mode_allow_unsigned=dev_mode_allow_unsigned)

    # ------------------------------------------------------------------ #
    # 注册与发现
    # ------------------------------------------------------------------ #

    def register(
        self,
        manifest: PluginManifest,
        *,
        artifact: bytes | None = None,
        signature: PluginSignature | None = None,
    ) -> str:
        """校验并注册插件，返回 plugin_id。

        注册前先按 :class:`PluginManifest` 重新校验清单（构造后被改动、或未经校验构造的
        清单同样会被拒绝），记录中保存的是校验后的副本；再用受信任的公钥校验签名：重新
        计算制品内容与清单的摘要，并校验算法与签名本身（见
        :meth:`forgemesh.plugin.signing.PluginSigner.verify`）。没有签名的插件只在开发模式下
        注册，并记录告警。

        plugin_id 由 ``metadata.id`` 与 ``metadata.version`` 组合，
        支持同一插件多版本并存（ADR-011）。重复注册同一 plugin_id 抛
        :class:`ConflictError`。

        Args:
            manifest: 插件清单。
            artifact: 制品内容字节（从制品存储读取）；有签名时必须提供。
            signature: 随制品分发的签名文档；``None`` 表示未签名。

        Returns:
            plugin_id 字符串。

        Raises:
            PluginRejectedError: 清单非法；插件未签名且未开启开发模式；有签名但没有制品内容；
                或签名校验失败。
            ConflictError: 同一 plugin_id 已注册。
        """
        try:
            checked = PluginManifest.model_validate(
                manifest.model_dump(mode="json", warnings=False)
            )
        except (ValidationError, PydanticSerializationError) as exc:
            raise PluginRejectedError(f"invalid plugin manifest: {exc}") from exc
        plugin_id = f"{checked.metadata.id}:{checked.metadata.version}"
        signing_key_id, artifact_digest = self._verify_signature(
            plugin_id, checked, artifact, signature
        )
        if plugin_id in self._plugins:
            raise ConflictError(f"plugin already registered: {plugin_id}")
        record = PluginRecord(
            plugin_id=plugin_id,
            manifest=checked,
            signing_key_id=signing_key_id,
            artifact_digest=artifact_digest,
        )
        self._plugins[plugin_id] = record
        return plugin_id

    def _verify_signature(
        self,
        plugin_id: str,
        manifest: PluginManifest,
        artifact: bytes | None,
        signature: PluginSignature | None,
    ) -> tuple[str | None, str | None]:
        if signature is None:
            if not self._dev_mode_allow_unsigned:
                raise PluginRejectedError(
                    f"plugin {plugin_id} rejected: it is unsigned and development mode is off"
                )
            logger.warning("registering unsigned plugin %s (development mode)", plugin_id)
            return None, None
        if artifact is None:
            raise PluginRejectedError(
                f"plugin {plugin_id} rejected: the artifact content is required to verify "
                "its signature"
            )
        try:
            key_id = self._signer.verify(artifact, manifest, signature, self._trusted_keys)
        except SigningError as exc:
            raise PluginRejectedError(f"plugin {plugin_id} rejected: {exc}") from exc
        return key_id, signature.artifact_digest

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

    def effective_permissions(self, plugin_id: str) -> frozenset[Permission]:
        """插件实际拥有的权限：清单申请、并且管理员已授予（:class:`PluginGrant`）的权限。

        Raises:
            NotFoundError: 插件未注册。
        """
        manifest = self.get(plugin_id).manifest
        granted = self._grants.get(manifest.metadata.id, frozenset())
        return frozenset(manifest.spec.permissions) & granted

    def check_permissions(self, plugin_id: str, required: Iterable[str]) -> bool:
        """校验插件是否拥有所需权限。

        按 ``required ⊆ 实际权限`` 判定，实际权限见 :meth:`effective_permissions`：
        清单自己申请、但管理员没有授予的权限不生效。空 ``required`` 恒通过。

        Args:
            plugin_id: 插件标识。
            required: 运行时所需权限（:class:`Permission` 词表中的名称）。

        Returns:
            ``True`` 表示权限充足。

        Raises:
            NotFoundError: 插件未注册。
            ValueError: ``required`` 中有词表之外的权限名。
        """
        effective = self.effective_permissions(plugin_id)
        return {Permission(name) for name in required} <= effective

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


__all__ = [
    "PluginGrant",
    "PluginManager",
    "PluginPolicy",
    "PluginPolicyError",
    "PluginRecord",
    "PluginRejectedError",
    "load_plugin_policy",
]
