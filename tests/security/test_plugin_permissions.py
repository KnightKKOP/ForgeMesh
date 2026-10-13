"""插件权限安全测试。

验证插件权限隔离机制，覆盖供应链安全与运行时隔离边界：

* **UNTRUSTED 插件**：不能使用 ``database.access``，不能申请 ``platform.admin``。
* **TRUSTED 插件**：拥有完整访问权限（IN_PROCESS 模式，全部权限可用）。
* **RESTRICTED_RPC 模式**：不可使用 ``database.access`` / ``filesystem.access`` /
  ``network.egress``。
* **权限名**：只接受 Permission 词表中的名称。
* **未签名插件**：被拒绝加载（SigningError）。
* **摘要不符插件**：被拒绝加载（SigningError）。
* **升级不变量（IVT-8）**：升级不改变既有实例的执行定义。

对齐 development-plan 5.7 / ADR-006 / IVT-8。
"""

from __future__ import annotations

import pytest
from forgemesh.plugin.isolation import (
    IsolationError,
    IsolationGuard,
    IsolationMode,
    PluginVersionCompat,
    TrustLevel,
)
from forgemesh.plugin.manifest import PluginManifest, PluginMeta, PluginSpec
from forgemesh.plugin.permissions import Permission
from forgemesh.plugin.signing import (
    PluginSigner,
    SigningError,
    generate_private_key,
    load_trusted_keys,
    public_key_pem,
)

_SIGNER = PluginSigner()
_PRIVATE_KEY = generate_private_key()
_TRUSTED = load_trusted_keys([public_key_pem(_PRIVATE_KEY)])
_MANIFEST = PluginManifest(
    metadata=PluginMeta(id="forgemesh.plugin.example", version="1.0.0"),
    spec=PluginSpec(category="policy"),
)


# --------------------------------------------------------------------------- #
# UNTRUSTED 插件权限隔离
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_untrusted_plugin_no_database_access() -> None:
    """UNTRUSTED 插件不能使用 database.access：check_access 抛 IsolationError。"""
    guard = IsolationGuard()

    with pytest.raises(IsolationError, match="'database.access' not available"):
        guard.check_access(TrustLevel.UNTRUSTED, "database.access")


@pytest.mark.unit
def test_untrusted_plugin_no_admin_permission() -> None:
    """UNTRUSTED 插件不能申请 platform.admin：validate_manifest 抛 IsolationError。"""
    guard = IsolationGuard()

    with pytest.raises(IsolationError, match=r"cannot request .*'platform\.admin'"):
        guard.validate_manifest(TrustLevel.UNTRUSTED, ["resource.read", "platform.admin"])


@pytest.mark.unit
@pytest.mark.parametrize("legacy_name", ["database", "admin", "network", "metrics:read"])
def test_free_form_permission_names_rejected(legacy_name: str) -> None:
    """词表外的权限名（含旧版自由命名）在任何信任级别下都报错，不会被当作普通权限放行。"""
    guard = IsolationGuard()

    for trust_level in TrustLevel:
        with pytest.raises(IsolationError, match="unknown permission"):
            guard.check_access(trust_level, legacy_name)
        with pytest.raises(IsolationError, match="unknown permission"):
            guard.validate_manifest(trust_level, [legacy_name])


# --------------------------------------------------------------------------- #
# TRUSTED 插件完整访问
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_trusted_plugin_full_access() -> None:
    """TRUSTED 插件有完整访问权限：IN_PROCESS 模式，全部权限可用，任意权限可申请。"""
    guard = IsolationGuard()

    # TRUSTED → IN_PROCESS 模式
    assert guard.get_mode(TrustLevel.TRUSTED) == IsolationMode.IN_PROCESS

    # 可使用全部权限（含直接访问宿主资源的权限）
    for permission in Permission:
        guard.check_access(TrustLevel.TRUSTED, permission)  # 不抛异常

    # 可申请任意权限（含 database.access / platform.admin）
    guard.validate_manifest(
        TrustLevel.TRUSTED, ["database.access", "platform.admin", "resource.read"]
    )


# --------------------------------------------------------------------------- #
# RESTRICTED_RPC 模式无直连 DB
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_restricted_rpc_no_direct_db() -> None:
    """RESTRICTED_RPC 模式无直连 DB：UNTRUSTED / VERIFIED 均禁止直接访问宿主资源的权限。"""
    guard = IsolationGuard()

    # UNTRUSTED → RESTRICTED_RPC
    assert guard.get_mode(TrustLevel.UNTRUSTED) == IsolationMode.RESTRICTED_RPC
    # VERIFIED → RESTRICTED_RPC
    assert guard.get_mode(TrustLevel.VERIFIED) == IsolationMode.RESTRICTED_RPC

    # 两个受限信任级别均禁止 database.access / filesystem.access / network.egress
    for trust_level in (TrustLevel.UNTRUSTED, TrustLevel.VERIFIED):
        for permission in ("database.access", "filesystem.access", "network.egress"):
            with pytest.raises(IsolationError, match=f"'{permission}' not available"):
                guard.check_access(trust_level, permission)

    # 经宿主 API 的权限仍可使用
    guard.check_access(TrustLevel.UNTRUSTED, "resource.read")
    guard.check_access(TrustLevel.VERIFIED, "metering.read")


# --------------------------------------------------------------------------- #
# 未签名插件被拒绝
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_unsigned_plugin_rejected() -> None:
    """未签名插件被拒绝加载：verify 抛 SigningError("unsigned plugin rejected")。"""
    with pytest.raises(SigningError, match="unsigned plugin rejected"):
        _SIGNER.verify(b"plugin payload", _MANIFEST, None, _TRUSTED)


# --------------------------------------------------------------------------- #
# 摘要不符插件被拒绝
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_digest_mismatch_rejected() -> None:
    """签名后被篡改的制品被拒绝加载：verify 从内容重算摘要，抛 SigningError。"""
    signature = _SIGNER.sign(b"plugin payload", _MANIFEST, _PRIVATE_KEY)

    with pytest.raises(SigningError, match="artifact digest mismatch"):
        _SIGNER.verify(b"tampered payload", _MANIFEST, signature, _TRUSTED)


# --------------------------------------------------------------------------- #
# 插件升级不变量（IVT-8）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_plugin_upgrade_ivt8() -> None:
    """升级不改变既有实例执行定义（IVT-8）。

    运行实例始终按其记录的插件版本执行；新版本仅作用于后续新建实例。
    """
    compat = PluginVersionCompat()

    # 既有实例创建时记录的插件版本
    recorded_version = "1.0.0"
    target_version = "2.0.0"

    # 评估破坏性升级
    check = compat.check_upgrade(recorded_version, target_version)
    assert check.breaking is True
    assert "IVT-8" in check.reason

    # 既有实例在升级后仍按原记录版本执行（不变）
    resolved = compat.instance_version_after_upgrade(recorded_version, target_version)
    assert resolved == recorded_version

    # 非破坏性升级同样不改变既有实例版本
    recorded_minor = "1.2.0"
    target_minor = "1.3.0"
    check_minor = compat.check_upgrade(recorded_minor, target_minor)
    assert check_minor.breaking is False
    resolved_minor = compat.instance_version_after_upgrade(recorded_minor, target_minor)
    assert resolved_minor == recorded_minor

    # 模拟完整场景：既有实例记录未被升级静默改变
    instance_record: dict[str, str] = {
        "instance_id": "wf-001",
        "plugin_version": recorded_version,
    }
    original = instance_record["plugin_version"]
    # 升级发生后，既有实例解析出的版本仍是原版本
    assert compat.instance_version_after_upgrade(
        instance_record["plugin_version"], target_version
    ) == original
    # 实例记录本身未被修改
    assert instance_record["plugin_version"] == original
