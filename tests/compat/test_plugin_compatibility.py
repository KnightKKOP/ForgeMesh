"""ForgeMesh 插件兼容性与安全认证用例（WP-5.3）。

覆盖：

* **核心 API 版本兼容**：PluginManifest apiVersion、PluginVersionCompat 语义版本兼容。
* **签名验证（安全认证）**：PluginSigner 拒绝未签名、摘要不符、签名公钥不受信任的制品。
* **隔离边界（安全认证）**：RESTRICTED_RPC 资源访问边界、UNTRUSTED 权限边界。
* **升级安全（IVT-8）**：升级不静默改变既有工作流实例的执行定义。
* **SDK 打包/发布兼容**：PluginSdk.pack/publish 端到端兼容性。

运行：``pytest tests/compat/test_plugin_compatibility.py -v``。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from forgemesh.plugin.isolation import (
    IsolationError,
    IsolationGuard,
    IsolationMode,
    IsolationPolicy,
    PluginVersionCompat,
    TrustLevel,
)
from forgemesh.plugin.manifest import PluginManifest, PluginMeta, PluginSpec
from forgemesh.plugin.sdk import PluginArtifact as SdkArtifact
from forgemesh.plugin.sdk import PluginSdk, PublishResult, ValidationResult
from forgemesh.plugin.signing import (
    PluginSigner,
    SigningError,
    generate_private_key,
    load_trusted_keys,
    public_key_pem,
)

#: 兼容性测试统一标记。
_compat = pytest.mark.contract

#: 签名测试使用的清单。
_SIGNED_MANIFEST = PluginManifest(
    metadata=PluginMeta(id="p", version="0.1.0"),
    spec=PluginSpec(category="policy"),
)


# ===========================================================================
# 1. 核心 API 版本兼容
# ===========================================================================


class TestCoreApiVersionCompat:
    """核心 API 版本兼容性。"""

    @_compat
    def test_manifest_default_api_version(self) -> None:
        """PluginManifest 默认 apiVersion 应为 v1alpha1。"""
        manifest = PluginManifest(
            metadata=PluginMeta(id="p", version="0.1.0"),
            spec=PluginSpec(category="policy"),
        )
        assert manifest.apiVersion == "forgemesh.io/v1alpha1"

    @_compat
    def test_manifest_accepts_future_api_version(self) -> None:
        """清单应接受显式指定的 apiVersion（向前兼容）。"""
        manifest = PluginManifest(
            apiVersion="forgemesh.io/v1beta1",
            metadata=PluginMeta(id="p", version="0.1.0"),
            spec=PluginSpec(category="policy"),
        )
        assert manifest.apiVersion == "forgemesh.io/v1beta1"

    @_compat
    def test_manifest_kind_is_stable(self) -> None:
        """PluginManifest kind 应固定为 PluginManifest。"""
        manifest = PluginManifest(
            metadata=PluginMeta(id="p", version="0.1.0"),
            spec=PluginSpec(category="policy"),
        )
        assert manifest.kind == "PluginManifest"


# ===========================================================================
# 2. 版本兼容性：PluginVersionCompat
# ===========================================================================


class TestPluginVersionCompat:
    """PluginVersionCompat 语义版本兼容与升级安全（IVT-8）。"""

    @_compat
    @pytest.mark.parametrize(
        ("plugin_ver", "required_ver", "expected"),
        [
            ("1.2.0", "1.0.0", True),
            ("1.0.0", "1.0.0", True),
            ("2.0.0", "1.0.0", False),
            ("0.9.0", "1.0.0", False),
            ("1.3.1", "1.3.0", True),
        ],
    )
    def test_is_compatible(
        self, plugin_ver: str, required_ver: str, expected: bool
    ) -> None:
        """同 major 且不低于所需版本则兼容。"""
        compat = PluginVersionCompat()
        assert compat.is_compatible(plugin_ver, required_ver) is expected

    @_compat
    def test_non_breaking_upgrade_compatible(self) -> None:
        """同 major 升级应为非破坏性且兼容。"""
        compat = PluginVersionCompat()
        check = compat.check_upgrade("1.2.0", "1.3.0")
        assert check.compatible is True
        assert check.breaking is False

    @_compat
    def test_breaking_upgrade_not_compatible(self) -> None:
        """跨 major 升级应为破坏性且不兼容。"""
        compat = PluginVersionCompat()
        check = compat.check_upgrade("1.2.0", "2.0.0")
        assert check.compatible is False
        assert check.breaking is True

    @_compat
    def test_downgrade_not_allowed(self) -> None:
        """降级应为破坏性且不兼容。"""
        compat = PluginVersionCompat()
        check = compat.check_upgrade("1.3.0", "1.2.0")
        assert check.compatible is False
        assert check.breaking is True

    @_compat
    def test_same_version_no_op(self) -> None:
        """同版本升级应为兼容且非破坏性。"""
        compat = PluginVersionCompat()
        check = compat.check_upgrade("1.0.0", "1.0.0")
        assert check.compatible is True
        assert check.breaking is False

    @_compat
    def test_ivt8_instance_version_pinned_after_upgrade(self) -> None:
        """IVT-8：升级后既有实例版本不变（不静默改变执行定义）。"""
        compat = PluginVersionCompat()
        recorded = "1.2.0"
        target = "1.3.0"
        assert compat.instance_version_after_upgrade(recorded, target) == recorded

    @_compat
    def test_ivt8_instance_version_pinned_after_breaking_upgrade(self) -> None:
        """IVT-8：破坏性升级后既有实例版本仍不变。"""
        compat = PluginVersionCompat()
        recorded = "1.2.0"
        target = "2.0.0"
        assert compat.instance_version_after_upgrade(recorded, target) == recorded


# ===========================================================================
# 3. 签名验证（安全认证）
# ===========================================================================


class TestSigningSecurity:
    """PluginSigner 安全认证：拒绝未签名、篡改与非受信任签名 + 正常验证通过。"""

    @_compat
    def test_compute_digest_format(self) -> None:
        """摘要应为 sha256:<hex> 格式。"""
        digest = PluginSigner.compute_digest(b"hello")
        assert digest.startswith("sha256:")
        assert len(digest) == len("sha256:") + 64

    @_compat
    def test_sign_and_verify_success(self) -> None:
        """签名后用对应的受信任公钥验证应通过。"""
        signer = PluginSigner()
        key = generate_private_key()
        content = b"plugin content"
        signature = signer.sign(content, _SIGNED_MANIFEST, key)
        trusted = load_trusted_keys([public_key_pem(key)])
        assert signer.verify(content, _SIGNED_MANIFEST, signature, trusted) == signature.key_id

    @_compat
    def test_unsigned_rejected(self) -> None:
        """未签名制品应被拒绝。"""
        signer = PluginSigner()
        with pytest.raises(SigningError, match="unsigned"):
            signer.verify(b"plugin content", _SIGNED_MANIFEST, None, {})

    @_compat
    def test_digest_mismatch_rejected(self) -> None:
        """摘要不符应被拒绝。"""
        signer = PluginSigner()
        key = generate_private_key()
        signature = signer.sign(b"plugin content", _SIGNED_MANIFEST, key)
        trusted = load_trusted_keys([public_key_pem(key)])
        # 篡改内容使摘要不匹配
        with pytest.raises(SigningError, match="digest"):
            signer.verify(b"tampered", _SIGNED_MANIFEST, signature, trusted)

    @_compat
    def test_signature_mismatch_rejected(self) -> None:
        """签名公钥不受信任（密钥不同）应被拒绝。"""
        signer = PluginSigner()
        content = b"plugin content"
        signature = signer.sign(content, _SIGNED_MANIFEST, generate_private_key())
        trusted = load_trusted_keys([public_key_pem(generate_private_key())])
        with pytest.raises(SigningError, match="not trusted"):
            signer.verify(content, _SIGNED_MANIFEST, signature, trusted)

    @_compat
    def test_digest_deterministic(self) -> None:
        """相同内容应产生相同摘要。"""
        d1 = PluginSigner.compute_digest(b"same")
        d2 = PluginSigner.compute_digest(b"same")
        assert d1 == d2

    @_compat
    def test_signer_fingerprint_stable(self) -> None:
        """相同密钥应产生相同 key id。"""
        signer = PluginSigner()
        key = generate_private_key()
        sig1 = signer.sign(b"content", _SIGNED_MANIFEST, key)
        sig2 = signer.sign(b"other content", _SIGNED_MANIFEST, key)
        assert sig1.key_id == sig2.key_id


# ===========================================================================
# 4. 隔离边界（安全认证）
# ===========================================================================


class TestIsolationBoundary:
    """隔离边界安全认证：权限使用边界 + 权限申请边界。"""

    @_compat
    def test_trusted_in_process_allows_database(self) -> None:
        """TRUSTED（IN_PROCESS）允许使用 database.access。"""
        guard = IsolationGuard()
        guard.check_access(TrustLevel.TRUSTED, "database.access")  # 不抛异常

    @_compat
    @pytest.mark.parametrize("trust", [TrustLevel.VERIFIED, TrustLevel.UNTRUSTED])
    def test_non_trusted_blocks_database(self, trust: TrustLevel) -> None:
        """VERIFIED/UNTRUSTED 应禁止直连 database。"""
        guard = IsolationGuard()
        with pytest.raises(IsolationError, match="'database.access' not available"):
            guard.check_access(trust, "database.access")

    @_compat
    @pytest.mark.parametrize("trust", [TrustLevel.VERIFIED, TrustLevel.UNTRUSTED])
    def test_non_trusted_blocks_filesystem(self, trust: TrustLevel) -> None:
        """VERIFIED/UNTRUSTED 应禁止直连 filesystem。"""
        guard = IsolationGuard()
        with pytest.raises(IsolationError, match="'filesystem.access' not available"):
            guard.check_access(trust, "filesystem.access")

    @_compat
    @pytest.mark.parametrize("trust", [TrustLevel.VERIFIED, TrustLevel.UNTRUSTED])
    def test_non_trusted_blocks_network(self, trust: TrustLevel) -> None:
        """VERIFIED/UNTRUSTED 应禁止直接对外发起网络连接。"""
        guard = IsolationGuard()
        with pytest.raises(IsolationError, match="'network.egress' not available"):
            guard.check_access(trust, "network.egress")

    @_compat
    def test_untrusted_combined_forbidden_permissions_rejected(self) -> None:
        """UNTRUSTED 同时申请 database.access + platform.admin 应被拒绝。"""
        guard = IsolationGuard()
        with pytest.raises(IsolationError, match=r"\['database\.access', 'platform\.admin'\]"):
            guard.validate_manifest(
                TrustLevel.UNTRUSTED, ["metering.read", "database.access", "platform.admin"]
            )

    @_compat
    def test_custom_isolation_policy(self) -> None:
        """自定义隔离策略应生效。"""
        custom_policy = IsolationPolicy(
            mapping={
                TrustLevel.TRUSTED: IsolationMode.RESTRICTED_RPC,
                TrustLevel.VERIFIED: IsolationMode.RESTRICTED_RPC,
                TrustLevel.UNTRUSTED: IsolationMode.RESTRICTED_RPC,
            }
        )
        guard = IsolationGuard(policy=custom_policy)
        # 自定义策略下 TRUSTED 也走 RESTRICTED_RPC
        with pytest.raises(IsolationError, match="not available in RESTRICTED_RPC mode"):
            guard.check_access(TrustLevel.TRUSTED, "database.access")


# ===========================================================================
# 5. SDK 打包/发布兼容性
# ===========================================================================


class TestSdkPackPublishCompat:
    """PluginSdk.pack/publish 端到端兼容性。"""

    @_compat
    def test_pack_produces_valid_artifact(self, tmp_path: Path) -> None:
        """pack 应产出含正确 name/version/digest/path 的制品。"""
        sdk = PluginSdk()
        sdk.generate_scaffold("compat-plugin", "decision", tmp_path)
        artifact = sdk.pack(tmp_path / "compat-plugin")
        assert artifact.name == "compat-plugin"
        assert artifact.version == "0.1.0"
        assert artifact.digest.startswith("sha256:")
        assert artifact.path.is_file()
        assert artifact.path.name == "compat-plugin-0.1.0.tar.gz"

    @_compat
    def test_pack_digest_matches_content(self, tmp_path: Path) -> None:
        """pack 的 digest 应与 tar.gz 内容一致。"""
        sdk = PluginSdk()
        sdk.generate_scaffold("digest-plugin", "runtime", tmp_path)
        artifact = sdk.pack(tmp_path / "digest-plugin")
        content = artifact.path.read_bytes()
        expected = f"sha256:{hashlib.sha256(content).hexdigest()}"
        assert artifact.digest == expected

    @_compat
    def test_publish_memory_protocol_success(self, tmp_path: Path) -> None:
        """memory:// 协议发布应成功。"""
        sdk = PluginSdk()
        sdk.generate_scaffold("pub-plugin", "qos", tmp_path)
        artifact = sdk.pack(tmp_path / "pub-plugin")
        result = sdk.publish(artifact, "memory://registry")
        assert result.success is True
        assert "pub-plugin" in result.url
        assert result.digest == artifact.digest

    @_compat
    def test_publish_invalid_protocol_fails(self, tmp_path: Path) -> None:
        """无效协议发布应返回 success=False。"""
        sdk = PluginSdk()
        sdk.generate_scaffold("fail-plugin", "metering", tmp_path)
        artifact = sdk.pack(tmp_path / "fail-plugin")
        result = sdk.publish(artifact, "ftp://registry")
        assert result.success is False
        assert result.url == ""

    @_compat
    def test_publish_http_unreachable_fails_gracefully(self, tmp_path: Path) -> None:
        """不可达 HTTP 注册中心应优雅返回 success=False（不抛异常）。"""
        sdk = PluginSdk()
        sdk.generate_scaffold("net-plugin", "gpu", tmp_path)
        artifact = sdk.pack(tmp_path / "net-plugin")
        # 使用一个确定不可达的地址（端口 1 通常被拒绝/超时）
        result = sdk.publish(artifact, "http://127.0.0.1:1")
        assert isinstance(result, PublishResult)
        assert result.success is False

    @_compat
    def test_validate_manifest_valid_result(self, tmp_path: Path) -> None:
        """合法 manifest 校验应返回 valid=True。"""
        sdk = PluginSdk()
        sdk.generate_scaffold("valid-plugin", "workflow", tmp_path)
        result = sdk.validate_manifest(tmp_path / "valid-plugin" / "manifest.yaml")
        assert isinstance(result, ValidationResult)
        assert result.valid is True
        assert result.errors == []

    @_compat
    def test_validate_manifest_missing_file(self, tmp_path: Path) -> None:
        """缺失 manifest 文件应返回 valid=False。"""
        sdk = PluginSdk()
        result = sdk.validate_manifest(tmp_path / "nonexistent.yaml")
        assert result.valid is False
        assert len(result.errors) == 1

    @_compat
    def test_sdk_artifact_dataclass_fields(self) -> None:
        """SdkArtifact 数据类应含 name/version/digest/path 四字段。"""
        artifact = SdkArtifact(name="p", version="0.1.0", digest="sha256:x", path=Path("/tmp"))
        assert artifact.name == "p"
        assert artifact.version == "0.1.0"
        assert artifact.digest == "sha256:x"
        assert artifact.path == Path("/tmp")
