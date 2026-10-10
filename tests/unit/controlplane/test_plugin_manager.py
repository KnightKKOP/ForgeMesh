"""``forgemesh.controlplane.plugin_manager`` 单元测试。

覆盖 PluginManager 契约实现的核心语义：

* 注册与发现（register / get / discover）；
* 生命周期状态机（合法转移 / 非法转移抛 InvalidStateTransition / 终态）；
* 能力发现（get_capabilities）；
* 健康状态与生命周期分离（set_health / get_health）；
* 权限校验（最小权限原则：required ⊆ granted）；
* 版本管理（get_version / is_compatible）；
* 已安装列表（list_installed）。
"""

from __future__ import annotations

import pytest
from forgemesh.controlplane.plugin_manager import PluginManager, PluginRecord
from forgemesh.core.errors import ConflictError, InvalidStateTransition, NotFoundError
from forgemesh.plugin.manifest import (
    PluginCategory,
    PluginLifecycleState,
    PluginManifest,
    PluginMeta,
    PluginSpec,
)

# ---------------------------------------------------------------------- #
# 测试辅助
# ---------------------------------------------------------------------- #


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
            permissions=permissions if permissions is not None else ["device:read"],
            compatibility=compatibility if compatibility is not None else {},
        ),
    )


# ---------------------------------------------------------------------- #
# 注册与发现
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_register_and_get() -> None:
    """注册后可通过 plugin_id 获取记录，且初始状态为 Discovered。"""
    mgr = PluginManager()
    manifest = _make_manifest()
    pid = mgr.register(manifest)
    assert pid == "forgemesh.hardware.gpu-nvidia:0.1.0"

    record = mgr.get(pid)
    assert record.plugin_id == pid
    assert record.manifest is manifest
    assert record.lifecycle_state == PluginLifecycleState.DISCOVERED


@pytest.mark.unit
def test_register_duplicate_conflict() -> None:
    """重复注册同一 plugin_id 抛 ConflictError。"""
    mgr = PluginManager()
    manifest = _make_manifest()
    mgr.register(manifest)

    with pytest.raises(ConflictError):
        mgr.register(manifest)


@pytest.mark.unit
def test_register_multi_version_coexist() -> None:
    """同一插件不同版本可并存（ADR-011）。"""
    mgr = PluginManager()
    pid_a = mgr.register(_make_manifest(version="0.1.0"))
    pid_b = mgr.register(_make_manifest(version="0.2.0"))
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
    mgr = PluginManager()
    mgr.register(_make_manifest(plugin_id="hw", category=PluginCategory.HARDWARE))
    mgr.register(_make_manifest(plugin_id="rt", category=PluginCategory.RUNTIME))
    mgr.register(
        _make_manifest(plugin_id="obs", category=PluginCategory.OBSERVABILITY)
    )

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
    mgr = PluginManager()
    mgr.register(_make_manifest(plugin_id="a"))
    mgr.register(_make_manifest(plugin_id="b", category=PluginCategory.RUNTIME))

    all_plugins = mgr.discover()
    assert len(all_plugins) == 2


# ---------------------------------------------------------------------- #
# 生命周期状态机
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_lifecycle_valid_transition() -> None:
    """合法转移链 Discovered→Validating→Installed→Enabled。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())

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
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())

    with pytest.raises(InvalidStateTransition):
        mgr.transition(pid, PluginLifecycleState.ENABLED)


@pytest.mark.unit
def test_lifecycle_invalid_transition_installed_to_discovered() -> None:
    """非法转移 Installed→Discovered 抛 InvalidStateTransition。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
    mgr.transition(pid, PluginLifecycleState.VALIDATING)
    mgr.transition(pid, PluginLifecycleState.INSTALLED)

    with pytest.raises(InvalidStateTransition):
        mgr.transition(pid, PluginLifecycleState.DISCOVERED)


@pytest.mark.unit
def test_lifecycle_disable_enable() -> None:
    """Enabled→Disabled→Enabled 往返。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
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
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
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
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
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
    mgr = PluginManager()
    pid = mgr.register(
        _make_manifest(provides=["gpu.discovery", "gpu.alloc", "gpu.alloc"])
    )
    caps = mgr.get_capabilities(pid)
    assert caps == {"gpu.discovery", "gpu.alloc"}  # 去重


@pytest.mark.unit
def test_capabilities_empty() -> None:
    """无能力声明时返回空集合。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest(provides=[]))
    assert mgr.get_capabilities(pid) == set()


# ---------------------------------------------------------------------- #
# 健康状态（与生命周期分离）
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_health_separate_from_lifecycle() -> None:
    """set_health 不影响 lifecycle_state，反之亦然。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
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
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
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
    """拥有所需权限返回 True。"""
    mgr = PluginManager()
    pid = mgr.register(
        _make_manifest(permissions=["device:read", "device:write", "metrics:scrape"])
    )
    assert mgr.check_permissions(pid, {"device:read"}) is True
    assert mgr.check_permissions(pid, {"device:read", "device:write"}) is True
    # 全部权限
    assert mgr.check_permissions(
        pid, {"device:read", "device:write", "metrics:scrape"}
    ) is True


@pytest.mark.unit
def test_check_permissions_denied() -> None:
    """缺少所需权限返回 False。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest(permissions=["device:read"]))
    assert mgr.check_permissions(pid, {"device:write"}) is False
    assert mgr.check_permissions(pid, {"device:read", "device:write"}) is False


@pytest.mark.unit
def test_check_permissions_empty_required() -> None:
    """空 required 恒通过（最小权限：无需求即无授予需求）。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest(permissions=[]))
    assert mgr.check_permissions(pid, set()) is True


@pytest.mark.unit
def test_check_permissions_not_found() -> None:
    """对未注册插件校验权限抛 NotFoundError。"""
    mgr = PluginManager()
    with pytest.raises(NotFoundError):
        mgr.check_permissions("ghost:0.0.0", {"device:read"})


# ---------------------------------------------------------------------- #
# 版本管理
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_get_version() -> None:
    """获取插件版本号（metadata.version）。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest(version="1.2.3"))
    assert mgr.get_version(pid) == "1.2.3"


@pytest.mark.unit
def test_is_compatible_via_api_version() -> None:
    """未声明 compatibility.coreApi 时回退到 manifest.apiVersion 精确匹配。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())  # apiVersion=forgemesh.io/v1alpha1
    assert mgr.is_compatible(pid, "forgemesh.io/v1alpha1") is True
    assert mgr.is_compatible(pid, "forgemesh.io/v1beta1") is False


@pytest.mark.unit
def test_is_compatible_via_core_api() -> None:
    """声明 compatibility.coreApi 时优先用它判定。"""
    mgr = PluginManager()
    pid = mgr.register(_make_manifest(compatibility={"coreApi": "v1alpha1"}))
    assert mgr.is_compatible(pid, "v1alpha1") is True
    assert mgr.is_compatible(pid, "v1beta1") is False


# ---------------------------------------------------------------------- #
# 已安装列表
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_installed() -> None:
    """list_installed 只列出 Installed/Enabled/Disabled 的插件。"""
    mgr = PluginManager()
    pid_a = mgr.register(_make_manifest(plugin_id="a"))
    pid_b = mgr.register(_make_manifest(plugin_id="b"))
    pid_c = mgr.register(_make_manifest(plugin_id="c"))

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
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
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
    mgr = PluginManager()
    pid = mgr.register(_make_manifest())
    record = mgr.get(pid)
    assert isinstance(record, PluginRecord)
    # 可序列化
    data = record.model_dump()
    assert data["plugin_id"] == pid
    assert data["lifecycle_state"] == PluginLifecycleState.DISCOVERED
