"""插件隔离宿主与升级兼容单元测试（对齐 development-plan 5.7 / ADR-006 / IVT-8）。

覆盖：
* TrustLevel / IsolationMode 枚举；
* IsolationPolicy 映射（TRUSTED→IN_PROCESS, UNTRUSTED→RESTRICTED_RPC）；
* IsolationGuard.check_access（IN_PROCESS 允许全部, RESTRICTED_RPC 禁止
  database/filesystem/network）；
* validate_manifest（UNTRUSTED 不允许 database/admin 权限）；
* PluginVersionCompat.is_compatible（兼容/不兼容）；
* check_upgrade（非破坏性/破坏性升级）；
* IVT-8：升级后既有实例按原版本执行。
"""

from __future__ import annotations

import pytest
from forgemesh.plugin.isolation import (
    IsolationError,
    IsolationGuard,
    IsolationMode,
    IsolationPolicy,
    PluginVersionCompat,
    TrustLevel,
    UpgradeCheck,
)


class TestEnums:
    """信任级别与隔离模式枚举。"""

    def test_trust_level_values(self) -> None:
        assert TrustLevel.TRUSTED == 0
        assert TrustLevel.VERIFIED == 1
        assert TrustLevel.UNTRUSTED == 2

    def test_isolation_mode_values(self) -> None:
        assert IsolationMode.IN_PROCESS == 0
        assert IsolationMode.RESTRICTED_RPC == 1

    def test_trust_level_ordering(self) -> None:
        assert TrustLevel.TRUSTED < TrustLevel.VERIFIED < TrustLevel.UNTRUSTED


class TestIsolationPolicy:
    """信任级别到隔离模式的映射。"""

    def test_default_mapping_trusted_to_in_process(self) -> None:
        policy = IsolationPolicy()

        assert policy.resolve(TrustLevel.TRUSTED) == IsolationMode.IN_PROCESS

    def test_default_mapping_untrusted_to_restricted_rpc(self) -> None:
        policy = IsolationPolicy()

        assert policy.resolve(TrustLevel.UNTRUSTED) == IsolationMode.RESTRICTED_RPC

    def test_default_mapping_verified_to_restricted_rpc(self) -> None:
        policy = IsolationPolicy()

        assert policy.resolve(TrustLevel.VERIFIED) == IsolationMode.RESTRICTED_RPC

    def test_custom_mapping(self) -> None:
        policy = IsolationPolicy(
            mapping={TrustLevel.TRUSTED: IsolationMode.RESTRICTED_RPC}
        )

        assert policy.resolve(TrustLevel.TRUSTED) == IsolationMode.RESTRICTED_RPC


class TestIsolationGuardCheckAccess:
    """资源访问校验。"""

    def test_in_process_allows_all_resources(self) -> None:
        guard = IsolationGuard()

        for resource in ("database", "filesystem", "network", "config", "anything"):
            guard.check_access(TrustLevel.TRUSTED, resource)  # 不抛异常

    def test_restricted_rpc_blocks_database(self) -> None:
        guard = IsolationGuard()

        with pytest.raises(IsolationError, match="database"):
            guard.check_access(TrustLevel.UNTRUSTED, "database")

    def test_restricted_rpc_blocks_filesystem(self) -> None:
        guard = IsolationGuard()

        with pytest.raises(IsolationError, match="filesystem"):
            guard.check_access(TrustLevel.UNTRUSTED, "filesystem")

    def test_restricted_rpc_blocks_network(self) -> None:
        guard = IsolationGuard()

        with pytest.raises(IsolationError, match="network"):
            guard.check_access(TrustLevel.UNTRUSTED, "network")

    def test_restricted_rpc_allows_other_resources(self) -> None:
        guard = IsolationGuard()

        guard.check_access(TrustLevel.UNTRUSTED, "config")
        guard.check_access(TrustLevel.UNTRUSTED, "metrics")
        guard.check_access(TrustLevel.VERIFIED, "config")

    def test_get_mode(self) -> None:
        guard = IsolationGuard()

        assert guard.get_mode(TrustLevel.TRUSTED) == IsolationMode.IN_PROCESS
        assert guard.get_mode(TrustLevel.UNTRUSTED) == IsolationMode.RESTRICTED_RPC
        assert guard.get_mode(TrustLevel.VERIFIED) == IsolationMode.RESTRICTED_RPC


class TestValidateManifest:
    """权限声明校验。"""

    def test_untrusted_cannot_declare_database(self) -> None:
        guard = IsolationGuard()

        with pytest.raises(IsolationError, match="database"):
            guard.validate_manifest(TrustLevel.UNTRUSTED, ["database", "read"])

    def test_untrusted_cannot_declare_admin(self) -> None:
        guard = IsolationGuard()

        with pytest.raises(IsolationError, match="admin"):
            guard.validate_manifest(TrustLevel.UNTRUSTED, ["admin"])

    def test_untrusted_can_declare_safe_permissions(self) -> None:
        guard = IsolationGuard()

        guard.validate_manifest(TrustLevel.UNTRUSTED, ["read", "metrics", "log"])

    def test_trusted_can_declare_any_permission(self) -> None:
        guard = IsolationGuard()

        guard.validate_manifest(TrustLevel.TRUSTED, ["database", "admin"])

    def test_verified_can_declare_database(self) -> None:
        """VERIFIED 级别不受 UNTRUSTED 权限限制。"""
        guard = IsolationGuard()

        guard.validate_manifest(TrustLevel.VERIFIED, ["database"])


class TestIsCompatible:
    """语义版本兼容检查。"""

    def setup_method(self) -> None:
        self.compat = PluginVersionCompat()

    def test_compatible_same_version(self) -> None:
        assert self.compat.is_compatible("1.2.3", "1.2.3") is True

    def test_compatible_higher_patch(self) -> None:
        assert self.compat.is_compatible("1.2.5", "1.2.3") is True

    def test_compatible_higher_minor(self) -> None:
        assert self.compat.is_compatible("1.3.0", "1.2.0") is True

    def test_incompatible_lower_version(self) -> None:
        assert self.compat.is_compatible("1.2.1", "1.2.3") is False

    def test_incompatible_different_major(self) -> None:
        assert self.compat.is_compatible("2.0.0", "1.5.0") is False

    def test_incompatible_major_2_vs_1(self) -> None:
        assert self.compat.is_compatible("2.1.0", "1.9.9") is False


class TestCheckUpgrade:
    """升级检查。"""

    def setup_method(self) -> None:
        self.compat = PluginVersionCompat()

    def test_non_breaking_patch_upgrade(self) -> None:
        result = self.compat.check_upgrade("1.2.0", "1.2.3")

        assert isinstance(result, UpgradeCheck)
        assert result.compatible is True
        assert result.breaking is False
        assert "non-breaking" in result.reason

    def test_non_breaking_minor_upgrade(self) -> None:
        result = self.compat.check_upgrade("1.2.0", "1.3.0")

        assert result.compatible is True
        assert result.breaking is False

    def test_breaking_major_upgrade(self) -> None:
        result = self.compat.check_upgrade("1.5.0", "2.0.0")

        assert result.compatible is False
        assert result.breaking is True
        assert "IVT-8" in result.reason

    def test_same_version(self) -> None:
        result = self.compat.check_upgrade("1.2.3", "1.2.3")

        assert result.compatible is True
        assert result.breaking is False
        assert "same version" in result.reason

    def test_downgrade_rejected(self) -> None:
        result = self.compat.check_upgrade("2.0.0", "1.0.0")

        assert result.compatible is False
        assert result.breaking is True
        assert "downgrade" in result.reason


class TestIVT8ExistingInstancesPinned:
    """IVT-8：升级不静默改变既有工作流实例的执行定义。"""

    def setup_method(self) -> None:
        self.compat = PluginVersionCompat()

    def test_existing_instance_keeps_recorded_version_on_breaking_upgrade(self) -> None:
        """破坏性升级后，既有实例仍按原记录版本执行。"""
        recorded = "1.0.0"
        target = "2.0.0"
        check = self.compat.check_upgrade(recorded, target)

        assert check.breaking is True
        # 既有实例版本不变
        assert self.compat.instance_version_after_upgrade(recorded, target) == recorded

    def test_existing_instance_keeps_recorded_version_on_non_breaking_upgrade(self) -> None:
        """非破坏性升级后，既有实例仍按原记录版本执行（IVT-8 不变量）。"""
        recorded = "1.2.0"
        target = "1.3.0"
        check = self.compat.check_upgrade(recorded, target)

        assert check.breaking is False
        assert self.compat.instance_version_after_upgrade(recorded, target) == recorded

    def test_upgrade_does_not_silently_change_running_instance(self) -> None:
        """模拟运行实例记录定义/插件版本：升级后既有实例定义不变。"""
        # 既有实例创建时记录的插件版本
        instance_record = {
            "instance_id": "wf-001",
            "definition_version": "2026-01-01-v1",
            "plugin_version": "1.0.0",
        }
        original_plugin_version = instance_record["plugin_version"]

        # 发生破坏性升级 1.0.0 -> 2.0.0
        check = self.compat.check_upgrade("1.0.0", "2.0.0")
        assert check.breaking is True

        # 既有实例按原版本执行（IVT-8）
        resolved = self.compat.instance_version_after_upgrade(
            instance_record["plugin_version"], "2.0.0"
        )
        assert resolved == original_plugin_version
        # 实例记录未被升级静默改变
        assert instance_record["plugin_version"] == original_plugin_version
