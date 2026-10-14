"""``forgemesh.controlplane.plugin_manager`` 单元测试。

覆盖 PluginManager 契约实现的核心语义：

* 注册校验（签名、篡改检测、开发模式、清单重新校验）与插件策略文件；
* 注册与发现（register / get / discover）；
* 生命周期状态机（合法转移 / 非法转移抛 InvalidStateTransition / 终态）；
* 能力发现（get_capabilities）；
* 健康状态与生命周期分离（set_health / get_health）；
* 权限校验（最小权限原则：required ⊆ 申请 ∩ 授予）；
* 版本管理（get_version / is_compatible）；
* 已安装列表（list_installed）。
"""

from __future__ import annotations

import json
import logging

import pytest
from forgemesh.controlplane.plugin_manager import (
    PluginGrant,
    PluginManager,
    PluginPolicy,
    PluginPolicyError,
    PluginRecord,
    PluginRejectedError,
    load_plugin_policy,
)
from forgemesh.core.errors import ConflictError, InvalidStateTransition, NotFoundError
from forgemesh.plugin.manifest import (
    PluginCategory,
    PluginLifecycleState,
    PluginManifest,
    PluginMeta,
    PluginSpec,
)
from forgemesh.plugin.permissions import Permission
from forgemesh.plugin.signing import (
    PluginSignature,
    PluginSigner,
    generate_private_key,
    load_public_key,
    public_key_id,
    public_key_pem,
)

# ---------------------------------------------------------------------- #
# 测试辅助
# ---------------------------------------------------------------------- #

#: 测试用的插件作者私钥：每次运行时生成，不写入磁盘。
_AUTHOR_KEY = generate_private_key()
_TRUSTED_KEY = public_key_pem(_AUTHOR_KEY).decode("ascii")


def _make_manifest(
    plugin_id: str = "forgemesh.hardware.gpu-nvidia",
    version: str = "0.1.0",
    category: PluginCategory = PluginCategory.HARDWARE,
    provides: list = None,
    permissions: list = None,
    compatibility: dict = None,
) -> PluginManifest:
    """构造测试用 PluginManifest。"""
    return PluginManifest(
        apiVersion="forgemesh.io/v1alpha1",
        kind="PluginManifest",
        metadata=PluginMeta(id=plugin_id, version=version),
        spec=PluginSpec(
            category=category,
            provides=provides if provides is not None else ["gpu.discovery"],
            permissions=permissions if permissions is not None else ["device.read"],
            compatibility=compatibility if compatibility is not None else {},
        ),
    )


def _manager(*grants: PluginGrant, dev_mode_allow_unsigned: bool = False) -> PluginManager:
    """构造只信任 ``_AUTHOR_KEY`` 的插件管理器。"""
    policy = PluginPolicy(trustedKeys=[_TRUSTED_KEY], grants=grants)
    return PluginManager(policy, dev_mode_allow_unsigned=dev_mode_allow_unsigned)


def _grant(plugin_id: str, *permissions: str) -> PluginGrant:
    return PluginGrant(pluginId=plugin_id, permissions=permissions)


def _artifact(manifest: PluginManifest) -> bytes:
    """制品内容的占位字节：签名校验只关心内容与签名是否一致。"""
    return f"{manifest.metadata.id}-{manifest.metadata.version}.tar.gz".encode()


def _sign(manifest: PluginManifest, key: bytes = _AUTHOR_KEY) -> PluginSignature:
    return PluginSigner().sign(_artifact(manifest), manifest, key)


def _register(mgr: PluginManager, manifest: PluginManifest) -> str:
    """按正常发布路径注册：制品内容与签名一起提交。"""
    return mgr.register(manifest, artifact=_artifact(manifest), signature=_sign(manifest))


# ---------------------------------------------------------------------- #
# 注册校验：签名、开发模式与清单校验
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_register_records_signing_key_and_artifact_digest() -> None:
    """签名校验通过后，记录签名公钥的 key id 与制品摘要。"""
    mgr = _manager()
    manifest = _make_manifest()
    record = mgr.get(_register(mgr, manifest))
    assert record.signing_key_id == public_key_id(load_public_key(_TRUSTED_KEY))
    assert record.artifact_digest == PluginSigner.compute_digest(_artifact(manifest))


@pytest.mark.unit
def test_register_rejects_tampered_artifact() -> None:
    """签名之后改动制品内容，注册被拒绝，插件不会进入注册表。"""
    mgr = _manager()
    manifest = _make_manifest()
    tampered = _artifact(manifest) + b"\x00"
    with pytest.raises(PluginRejectedError, match="artifact digest mismatch"):
        mgr.register(manifest, artifact=tampered, signature=_sign(manifest))
    assert mgr.discover() == []


@pytest.mark.unit
def test_register_rejects_manifest_changed_after_signing() -> None:
    """签名之后在清单里追加权限申请，清单摘要不符，注册被拒绝。"""
    mgr = _manager()
    signed = _make_manifest()
    widened = _make_manifest(permissions=["device.read", "platform.admin"])
    with pytest.raises(PluginRejectedError, match="manifest digest mismatch"):
        mgr.register(widened, artifact=_artifact(signed), signature=_sign(signed))


@pytest.mark.unit
@pytest.mark.parametrize("algorithm", ["hmac-sha256", "ED25519", "ed448"])
def test_register_rejects_unsupported_signature_algorithm(algorithm: str) -> None:
    """签名文档声明的算法不是 ed25519，注册被拒绝。"""
    mgr = _manager()
    manifest = _make_manifest()
    document = {**_sign(manifest).model_dump(by_alias=True), "algorithm": algorithm}
    signature = PluginSignature.model_validate(document)
    with pytest.raises(PluginRejectedError, match="unsupported signature algorithm"):
        mgr.register(manifest, artifact=_artifact(manifest), signature=signature)


@pytest.mark.unit
def test_register_rejects_untrusted_signing_key() -> None:
    """不受信任的公钥签名的插件被拒绝。"""
    mgr = _manager()
    manifest = _make_manifest()
    signature = _sign(manifest, key=generate_private_key())
    with pytest.raises(PluginRejectedError, match="is not trusted"):
        mgr.register(manifest, artifact=_artifact(manifest), signature=signature)


@pytest.mark.unit
def test_register_rejects_forged_signature() -> None:
    """摘要与当前制品相符、签名却属于另一个制品，注册被拒绝。"""
    mgr = _manager()
    manifest = _make_manifest()
    other = _sign(_make_manifest(version="9.9.9"))
    forged = _sign(manifest).model_copy(update={"signature": other.signature})
    with pytest.raises(PluginRejectedError, match="signature verification failed"):
        mgr.register(manifest, artifact=_artifact(manifest), signature=forged)


@pytest.mark.unit
def test_register_requires_artifact_content_with_signature() -> None:
    """只有签名、没有制品内容时无法重新计算摘要，注册被拒绝。"""
    mgr = _manager()
    manifest = _make_manifest()
    with pytest.raises(PluginRejectedError, match="artifact content is required"):
        mgr.register(manifest, signature=_sign(manifest))


@pytest.mark.unit
def test_unsigned_plugin_rejected_by_default() -> None:
    """开发模式默认关闭：未签名的插件被拒绝，带不带制品内容都一样。"""
    manifest = _make_manifest()
    for mgr in (PluginManager(), _manager()):
        with pytest.raises(PluginRejectedError, match="unsigned"):
            mgr.register(manifest)
        with pytest.raises(PluginRejectedError, match="unsigned"):
            mgr.register(manifest, artifact=_artifact(manifest))
        assert mgr.discover() == []


@pytest.mark.unit
def test_default_policy_trusts_no_key() -> None:
    """不传策略时没有受信任的公钥，签名的插件同样被拒绝。"""
    with pytest.raises(PluginRejectedError, match="is not trusted"):
        _register(PluginManager(), _make_manifest())


@pytest.mark.unit
def test_inline_manifest_signature_fields_are_ignored() -> None:
    """清单内已弃用的 artifactDigest / signature 代替不了签名文档，注册时也不比对。"""
    mgr = _manager()
    manifest = _make_manifest()
    manifest.spec.artifactDigest = PluginSigner.compute_digest(_artifact(manifest))
    manifest.spec.signature = "cosign:xyz"
    with pytest.raises(PluginRejectedError, match="unsigned"):
        mgr.register(manifest, artifact=_artifact(manifest))
    manifest.spec.artifactDigest = "sha256:" + "0" * 64
    record = mgr.get(_register(mgr, manifest))
    assert record.artifact_digest == PluginSigner.compute_digest(_artifact(manifest))


@pytest.mark.unit
def test_dev_mode_registers_unsigned_plugins(caplog: pytest.LogCaptureFixture) -> None:
    """开发模式下未签名的插件可以注册，开启开发模式和每次注册都会记录告警。"""
    with caplog.at_level(logging.WARNING, logger="forgemesh.controlplane.plugin_manager"):
        mgr = _manager(dev_mode_allow_unsigned=True)
        record = mgr.get(mgr.register(_make_manifest()))
    assert record.signing_key_id is None
    assert record.artifact_digest is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("development mode is on" in m for m in messages)
    assert any("unsigned plugin forgemesh.hardware.gpu-nvidia:0.1.0" in m for m in messages)


@pytest.mark.unit
def test_dev_mode_still_verifies_signatures() -> None:
    """开发模式只放行未签名的插件；带签名的插件照常校验，篡改依旧被拒绝。"""
    mgr = _manager(dev_mode_allow_unsigned=True)
    manifest = _make_manifest()
    with pytest.raises(PluginRejectedError, match="artifact digest mismatch"):
        mgr.register(manifest, artifact=b"tampered", signature=_sign(manifest))


@pytest.mark.unit
def test_register_revalidates_manifest() -> None:
    """构造后被改动、或未经校验构造的清单在注册时被拒绝，开发模式下同样如此。"""
    mgr = _manager(dev_mode_allow_unsigned=True)
    mutated = _make_manifest()
    mutated.spec.permissions.append("admin")
    with pytest.raises(PluginRejectedError, match=r"invalid plugin manifest[\s\S]*permissions"):
        mgr.register(mutated)
    unchecked = PluginManifest.model_construct(
        metadata=PluginMeta(id="forgemesh.policy.example", version="0.1.0"),
        spec=PluginSpec.model_construct(category=PluginCategory.POLICY, isolation="restricted-rpc"),
    )
    with pytest.raises(PluginRejectedError, match="isolation 'restricted-rpc' is not supported"):
        mgr.register(unchecked)
    assert mgr.discover() == []


@pytest.mark.unit
def test_registered_manifest_is_a_checked_copy() -> None:
    """记录保存校验过的副本：注册后再改调用方的清单，不影响记录和实际权限。"""
    mgr = _manager(_grant("forgemesh.hardware.gpu-nvidia", "device.read", "device.partition"))
    manifest = _make_manifest(permissions=["device.read"])
    pid = _register(mgr, manifest)
    manifest.spec.permissions.append(Permission.DEVICE_PARTITION)
    assert mgr.get(pid).manifest.spec.permissions == [Permission.DEVICE_READ]
    assert mgr.effective_permissions(pid) == {Permission.DEVICE_READ}


# ---------------------------------------------------------------------- #
# 插件策略文件
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_manager_from_policy_file(tmp_path) -> None:
    """策略文件配置受信任公钥与授予：签名的插件可以注册，授予的权限生效。"""
    policy_file = tmp_path / "plugin-policy.json"
    grant = {"pluginId": "forgemesh.hardware.gpu-nvidia", "permissions": ["device.read"]}
    policy = {"trustedKeys": [_TRUSTED_KEY], "grants": [grant]}
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    mgr = PluginManager.from_policy_file(policy_file)
    pid = _register(mgr, _make_manifest(permissions=["device.read", "device.partition"]))
    assert mgr.effective_permissions(pid) == {Permission.DEVICE_READ}


@pytest.mark.unit
@pytest.mark.parametrize(
    "policy",
    [
        pytest.param("not json", id="not-json"),
        pytest.param({"trustedKeys": ["not a key"]}, id="invalid-key"),
        pytest.param({"trustedKeys": [_TRUSTED_KEY, _TRUSTED_KEY]}, id="duplicate-key"),
        pytest.param({"trusted_keys": [_TRUSTED_KEY]}, id="snake-case-field"),
        pytest.param({"trustedKeys": [], "admins": []}, id="unknown-field"),
        pytest.param(
            {"grants": [{"pluginId": "forgemesh.a.b", "permissions": ["admin"]}]},
            id="unknown-permission",
        ),
        pytest.param(
            {"grants": [{"pluginId": "forgemesh.a.b", "permissions": ["device.read"] * 2}]},
            id="duplicate-permission",
        ),
        pytest.param({"grants": [{"pluginId": "forgemesh.a.b"}]}, id="grant-without-permissions"),
        pytest.param(
            {"grants": [{"pluginId": "forgemesh.a.b", "plugin_id": "x.y", "permissions": []}]},
            id="snake-case-grant-field",
        ),
        pytest.param(
            {"grants": [{"pluginId": "forgemesh.a.b:1.0.0", "permissions": []}]},
            id="versioned-plugin-id",
        ),
        pytest.param(
            {"grants": [{"pluginId": "forgemesh.a.b", "permissions": []}] * 2},
            id="duplicate-grant",
        ),
    ],
)
def test_load_plugin_policy_rejects_invalid_files(tmp_path, policy) -> None:
    policy_file = tmp_path / "plugin-policy.json"
    content = policy if isinstance(policy, str) else json.dumps(policy)
    policy_file.write_text(content, encoding="utf-8")
    with pytest.raises(PluginPolicyError, match="invalid plugin policy file"):
        load_plugin_policy(policy_file)
    with pytest.raises(PluginPolicyError):
        PluginManager.from_policy_file(policy_file)


@pytest.mark.unit
def test_load_plugin_policy_reports_unreadable_files(tmp_path) -> None:
    with pytest.raises(PluginPolicyError, match="cannot read plugin policy file"):
        load_plugin_policy(tmp_path / "missing.json")
    not_utf8 = tmp_path / "latin1.json"
    not_utf8.write_bytes('{"grants": [], "note": "é"}'.encode("latin-1"))
    with pytest.raises(PluginPolicyError, match="cannot read plugin policy file"):
        load_plugin_policy(not_utf8)


# ---------------------------------------------------------------------- #
# 注册与发现
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_register_and_get() -> None:
    """注册后可通过 plugin_id 获取记录，且初始状态为 Discovered。"""
    mgr = _manager()
    manifest = _make_manifest()
    pid = _register(mgr, manifest)
    assert pid == "forgemesh.hardware.gpu-nvidia:0.1.0"

    record = mgr.get(pid)
    assert record.plugin_id == pid
    assert record.manifest == manifest
    assert record.lifecycle_state == PluginLifecycleState.DISCOVERED


@pytest.mark.unit
def test_register_duplicate_conflict() -> None:
    """重复注册同一 plugin_id 抛 ConflictError。"""
    mgr = _manager()
    manifest = _make_manifest()
    _register(mgr, manifest)

    with pytest.raises(ConflictError):
        _register(mgr, manifest)


@pytest.mark.unit
def test_register_multi_version_coexist() -> None:
    """同一插件不同版本可并存（ADR-011）。"""
    mgr = _manager()
    pid_a = _register(mgr, _make_manifest(version="0.1.0"))
    pid_b = _register(mgr, _make_manifest(version="0.2.0"))
    assert pid_a != pid_b
    assert mgr.get(pid_a).manifest.metadata.version == "0.1.0"
    assert mgr.get(pid_b).manifest.metadata.version == "0.2.0"


@pytest.mark.unit
def test_get_not_found() -> None:
    """获取未注册插件抛 NotFoundError。"""
    mgr = PluginManager()
    with pytest.raises(NotFoundError):
        mgr.get("nonexistent:0.0.0")


@pytest.mark.unit
def test_discover_by_category() -> None:
    """按类别过滤已注册插件。"""
    mgr = _manager()
    _register(mgr, _make_manifest(plugin_id="hw", category=PluginCategory.HARDWARE))
    _register(mgr, _make_manifest(plugin_id="rt", category=PluginCategory.RUNTIME))
    _register(mgr, _make_manifest(plugin_id="obs", category=PluginCategory.OBSERVABILITY))

    hw = mgr.discover(PluginCategory.HARDWARE)
    assert len(hw) == 1
    assert hw[0].metadata.id == "hw"

    rt = mgr.discover(PluginCategory.RUNTIME)
    assert len(rt) == 1
    assert rt[0].metadata.id == "rt"

    # 不存在的类别
    assert mgr.discover(PluginCategory.POLICY) == []


@pytest.mark.unit
def test_discover_all() -> None:
    """不传 category 返回全部已注册插件。"""
    mgr = _manager()
    _register(mgr, _make_manifest(plugin_id="a"))
    _register(mgr, _make_manifest(plugin_id="b", category=PluginCategory.RUNTIME))

    all_plugins = mgr.discover()
    assert len(all_plugins) == 2


# ---------------------------------------------------------------------- #
# 生命周期状态机
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_lifecycle_valid_transition() -> None:
    """合法转移链 Discovered→Validating→Installed→Enabled。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())

    r1 = mgr.transition(pid, PluginLifecycleState.VALIDATING)
    assert r1.lifecycle_state == PluginLifecycleState.VALIDATING

    r2 = mgr.transition(pid, PluginLifecycleState.INSTALLED)
    assert r2.lifecycle_state == PluginLifecycleState.INSTALLED
    assert r2.installed_at is not None  # 进入 Installed 记录时间戳

    r3 = mgr.transition(pid, PluginLifecycleState.ENABLED)
    assert r3.lifecycle_state == PluginLifecycleState.ENABLED


@pytest.mark.unit
def test_lifecycle_invalid_transition() -> None:
    """非法转移 Discovered→Enabled 抛 InvalidStateTransition。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())

    with pytest.raises(InvalidStateTransition):
        mgr.transition(pid, PluginLifecycleState.ENABLED)


@pytest.mark.unit
def test_lifecycle_invalid_transition_installed_to_discovered() -> None:
    """非法转移 Installed→Discovered 抛 InvalidStateTransition。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    mgr.transition(pid, PluginLifecycleState.VALIDATING)
    mgr.transition(pid, PluginLifecycleState.INSTALLED)

    with pytest.raises(InvalidStateTransition):
        mgr.transition(pid, PluginLifecycleState.DISCOVERED)


@pytest.mark.unit
def test_lifecycle_disable_enable() -> None:
    """Enabled→Disabled→Enabled 往返。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    mgr.transition(pid, PluginLifecycleState.VALIDATING)
    mgr.transition(pid, PluginLifecycleState.INSTALLED)
    mgr.transition(pid, PluginLifecycleState.ENABLED)

    r = mgr.transition(pid, PluginLifecycleState.DISABLED)
    assert r.lifecycle_state == PluginLifecycleState.DISABLED

    r = mgr.transition(pid, PluginLifecycleState.ENABLED)
    assert r.lifecycle_state == PluginLifecycleState.ENABLED


@pytest.mark.unit
def test_lifecycle_uninstall_terminal() -> None:
    """Uninstalled 为终态，不可再转移。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    mgr.transition(pid, PluginLifecycleState.VALIDATING)
    mgr.transition(pid, PluginLifecycleState.INSTALLED)
    mgr.transition(pid, PluginLifecycleState.ENABLED)
    mgr.transition(pid, PluginLifecycleState.UNINSTALLED)

    # 终态：任何转移都应被拒
    with pytest.raises(InvalidStateTransition):
        mgr.transition(pid, PluginLifecycleState.ENABLED)
    with pytest.raises(InvalidStateTransition):
        mgr.transition(pid, PluginLifecycleState.DISABLED)


@pytest.mark.unit
def test_lifecycle_uninstall_from_disabled() -> None:
    """Disabled→Uninstalled 合法。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    mgr.transition(pid, PluginLifecycleState.VALIDATING)
    mgr.transition(pid, PluginLifecycleState.INSTALLED)
    mgr.transition(pid, PluginLifecycleState.ENABLED)
    mgr.transition(pid, PluginLifecycleState.DISABLED)
    r = mgr.transition(pid, PluginLifecycleState.UNINSTALLED)
    assert r.lifecycle_state == PluginLifecycleState.UNINSTALLED


@pytest.mark.unit
def test_lifecycle_transition_not_found() -> None:
    """对未注册插件转移抛 NotFoundError。"""
    mgr = PluginManager()
    with pytest.raises(NotFoundError):
        mgr.transition("ghost:0.0.0", PluginLifecycleState.VALIDATING)


# ---------------------------------------------------------------------- #
# 能力发现
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_capabilities() -> None:
    """获取插件声明的能力集合（spec.provides）。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest(provides=["gpu.discovery", "gpu.alloc", "gpu.alloc"]))
    caps = mgr.get_capabilities(pid)
    assert caps == {"gpu.discovery", "gpu.alloc"}  # 去重


@pytest.mark.unit
def test_capabilities_empty() -> None:
    """无能力声明时返回空集合。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest(provides=[]))
    assert mgr.get_capabilities(pid) == set()


# ---------------------------------------------------------------------- #
# 健康状态（与生命周期分离）
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_health_separate_from_lifecycle() -> None:
    """set_health 不影响 lifecycle_state，反之亦然。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    mgr.transition(pid, PluginLifecycleState.VALIDATING)
    mgr.transition(pid, PluginLifecycleState.INSTALLED)
    mgr.transition(pid, PluginLifecycleState.ENABLED)

    # 设置健康状态
    mgr.set_health(pid, "Degraded")
    assert mgr.get_health(pid) == "Degraded"

    # 生命周期状态不受影响
    record = mgr.get(pid)
    assert record.lifecycle_state == PluginLifecycleState.ENABLED

    # 转移生命周期不影响健康状态
    mgr.transition(pid, PluginLifecycleState.DISABLED)
    assert mgr.get_health(pid) == "Degraded"


@pytest.mark.unit
def test_get_health_default() -> None:
    """注册后默认健康状态为 Unknown。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    assert mgr.get_health(pid) == "Unknown"


@pytest.mark.unit
def test_set_health_not_found() -> None:
    """对未注册插件设置健康状态抛 NotFoundError。"""
    mgr = PluginManager()
    with pytest.raises(NotFoundError):
        mgr.set_health("ghost:0.0.0", "Healthy")


# ---------------------------------------------------------------------- #
# 权限校验（最小权限原则）
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_check_permissions_granted() -> None:
    """申请并且已获授予的权限返回 True。"""
    permissions = ["device.read", "device.partition", "metering.read"]
    mgr = _manager(_grant("forgemesh.hardware.gpu-nvidia", *permissions))
    pid = _register(mgr, _make_manifest(permissions=permissions))
    assert mgr.check_permissions(pid, {"device.read"}) is True
    assert mgr.check_permissions(pid, {"device.read", "device.partition"}) is True
    # 全部权限
    assert mgr.check_permissions(pid, {"device.read", "device.partition", "metering.read"}) is True


@pytest.mark.unit
def test_check_permissions_denied() -> None:
    """缺少所需权限返回 False。"""
    mgr = _manager(_grant("forgemesh.hardware.gpu-nvidia", "device.read"))
    pid = _register(mgr, _make_manifest(permissions=["device.read"]))
    assert mgr.check_permissions(pid, {"device.partition"}) is False
    assert mgr.check_permissions(pid, {"device.read", "device.partition"}) is False


@pytest.mark.unit
def test_check_permissions_empty_required() -> None:
    """空 required 恒通过（最小权限：无需求即无授予需求）。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest(permissions=[]))
    assert mgr.check_permissions(pid, set()) is True


@pytest.mark.unit
def test_check_permissions_not_found() -> None:
    """对未注册插件校验权限抛 NotFoundError。"""
    mgr = PluginManager()
    with pytest.raises(NotFoundError):
        mgr.check_permissions("ghost:0.0.0", {"device.read"})


# ---------------------------------------------------------------------- #
# 版本管理
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_get_version() -> None:
    """获取插件版本号（metadata.version）。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest(version="1.2.3"))
    assert mgr.get_version(pid) == "1.2.3"


@pytest.mark.unit
def test_is_compatible_via_api_version() -> None:
    """未声明 compatibility.coreApi 时回退到 manifest.apiVersion 精确匹配。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())  # apiVersion=forgemesh.io/v1alpha1
    assert mgr.is_compatible(pid, "forgemesh.io/v1alpha1") is True
    assert mgr.is_compatible(pid, "forgemesh.io/v1beta1") is False


@pytest.mark.unit
def test_is_compatible_via_core_api() -> None:
    """声明 compatibility.coreApi 时优先用它判定。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest(compatibility={"coreApi": "v1alpha1"}))
    assert mgr.is_compatible(pid, "v1alpha1") is True
    assert mgr.is_compatible(pid, "v1beta1") is False


# ---------------------------------------------------------------------- #
# 已安装列表
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_installed() -> None:
    """list_installed 只列出 Installed/Enabled/Disabled 的插件。"""
    mgr = _manager()
    pid_a = _register(mgr, _make_manifest(plugin_id="a"))
    pid_b = _register(mgr, _make_manifest(plugin_id="b"))
    pid_c = _register(mgr, _make_manifest(plugin_id="c"))

    # a: Discovered（未安装）
    # b: →Validating→Installed
    mgr.transition(pid_b, PluginLifecycleState.VALIDATING)
    mgr.transition(pid_b, PluginLifecycleState.INSTALLED)
    # c: →Validating→Installed→Enabled→Uninstalled
    mgr.transition(pid_c, PluginLifecycleState.VALIDATING)
    mgr.transition(pid_c, PluginLifecycleState.INSTALLED)
    mgr.transition(pid_c, PluginLifecycleState.ENABLED)
    mgr.transition(pid_c, PluginLifecycleState.UNINSTALLED)

    installed = mgr.list_installed()
    installed_ids = {r.plugin_id for r in installed}
    assert pid_b in installed_ids
    assert pid_a not in installed_ids  # Discovered
    assert pid_c not in installed_ids  # Uninstalled


@pytest.mark.unit
def test_list_installed_includes_disabled() -> None:
    """Disabled 状态也属于已安装。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    mgr.transition(pid, PluginLifecycleState.VALIDATING)
    mgr.transition(pid, PluginLifecycleState.INSTALLED)
    mgr.transition(pid, PluginLifecycleState.ENABLED)
    mgr.transition(pid, PluginLifecycleState.DISABLED)

    installed = mgr.list_installed()
    assert len(installed) == 1
    assert installed[0].plugin_id == pid


@pytest.mark.unit
def test_plugin_record_is_pydantic_model() -> None:
    """PluginRecord 是 pydantic BaseModel，支持模型操作。"""
    mgr = _manager()
    pid = _register(mgr, _make_manifest())
    record = mgr.get(pid)
    assert isinstance(record, PluginRecord)
    # 可序列化
    data = record.model_dump()
    assert data["plugin_id"] == pid
    assert data["lifecycle_state"] == PluginLifecycleState.DISCOVERED
