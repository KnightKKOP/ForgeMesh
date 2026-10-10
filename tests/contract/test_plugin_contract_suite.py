"""ForgeMesh 开放契约测试套件：插件 SDK 契约一致性（WP-5.3）。

本套件对第三方开放，用于校验插件实现与 ForgeMesh 核心契约的一致性：

* **PluginManifest Schema 校验**：合法/非法清单的接受与拒绝。
* **Provider 契约一致性**：6 种 kind 脚手架生成的 Provider 方法签名与返回结构。
* **插件生命周期状态机**：合法转移通过、非法转移抛异常。
* **权限模型**：信任级别与隔离模式的映射、UNTRUSTED 权限声明约束。

运行：``pytest tests/contract/test_plugin_contract_suite.py -v``。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from forgemesh.plugin.isolation import (
    IsolationError,
    IsolationGuard,
    IsolationMode,
    TrustLevel,
)
from forgemesh.plugin.manifest import (
    PLUGIN_TRANSITIONS,
    PluginCategory,
    PluginLifecycleState,
    PluginManifest,
    PluginMeta,
    PluginSpec,
    validate_plugin_transition,
)
from forgemesh.plugin.scaffold import KIND_CATEGORY_MAP, SUPPORTED_KINDS, ScaffoldGenerator
from pydantic import ValidationError

try:
    from forgemesh.core.errors import InvalidStateTransition
except ImportError:  # pragma: no cover
    from forgemesh.plugin.manifest import InvalidStateTransition  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 辅助：加载脚手架生成的 provider
# ---------------------------------------------------------------------------


def _load_provider_module(plugin_root: Path) -> Any:
    """按文件路径加载 ``provider.py``（兼容连字符目录名）。"""
    provider_path = plugin_root / "provider.py"
    spec = importlib.util.spec_from_file_location(
        f"_scaffold_{plugin_root.name}_provider", provider_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_manifest(**spec_overrides: Any) -> PluginManifest:
    """构造一个合法的 PluginManifest，允许覆盖 spec 字段。"""
    spec_fields: dict[str, Any] = {
        "category": PluginCategory.POLICY,
        "compatibility": {"coreApi": "v1alpha1"},
        "provides": ["test-plugin"],
        "permissions": ["metrics:read"],
        "isolation": "restricted-rpc",
        "entrypoint": "TestPlugin",
    }
    spec_fields.update(spec_overrides)
    return PluginManifest(
        metadata=PluginMeta(id="test-plugin", version="0.1.0"),
        spec=PluginSpec(**spec_fields),
    )


# ===========================================================================
# 1. PluginManifest Schema 校验
# ===========================================================================


class TestPluginManifestSchema:
    """PluginManifest Schema 契约：合法清单通过、非法清单被拒绝。"""

    @pytest.mark.contract
    def test_valid_manifest_accepted(self) -> None:
        """合法 manifest 应通过校验。"""
        manifest = _make_manifest()
        assert manifest.metadata.id == "test-plugin"
        assert manifest.spec.category == PluginCategory.POLICY

    @pytest.mark.contract
    @pytest.mark.parametrize("category", list(PluginCategory))
    def test_all_categories_accepted(self, category: PluginCategory) -> None:
        """所有 6 个 PluginCategory 枚举值都应被接受。"""
        manifest = _make_manifest(category=category)
        assert manifest.spec.category == category

    @pytest.mark.contract
    def test_missing_metadata_rejected(self) -> None:
        """缺失 metadata 应被拒绝。"""
        with pytest.raises(ValidationError):
            PluginManifest.model_validate(
                {"apiVersion": "forgemesh.io/v1alpha1", "kind": "PluginManifest", "spec": {}}
            )

    @pytest.mark.contract
    def test_invalid_category_rejected(self) -> None:
        """非法 category 字符串应被拒绝。"""
        with pytest.raises(ValidationError):
            PluginSpec(category="not-a-real-category")  # type: ignore[arg-type]

    @pytest.mark.contract
    def test_extra_spec_field_rejected(self) -> None:
        """spec 禁止额外字段（ConfigDict extra=forbid）。"""
        with pytest.raises(ValidationError):
            PluginSpec(category=PluginCategory.POLICY, unknownField="x")  # type: ignore[call-arg]

    @pytest.mark.contract
    def test_manifest_from_yaml_dict(self) -> None:
        """从 YAML 解析的字典应能构造 manifest。"""
        raw = {
            "apiVersion": "forgemesh.io/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"id": "my-plugin", "version": "1.0.0"},
            "spec": {
                "category": "runtime",
                "compatibility": {"coreApi": "v1alpha1"},
                "provides": ["my-plugin"],
                "isolation": "in-process",
            },
        }
        manifest = PluginManifest.model_validate(raw)
        assert manifest.metadata.version == "1.0.0"
        assert manifest.spec.isolation == "in-process"


# ===========================================================================
# 2. Provider 契约一致性
# ===========================================================================

#: 各 kind 的契约方法名与返回值必须包含的键。
_PROVIDER_CONTRACTS: dict[str, dict[str, list[str]]] = {
    "decision": {"evaluate": ["decision"]},
    "workflow": {"execute": ["status"]},
    "runtime": {"start": ["instanceId"]},
    "gpu": {"discover": []},  # 返回 list，无必含键
    "metering": {"record": ["recorded"]},
    "qos": {"apply": ["applied"]},
}


class TestProviderContract:
    """Provider 契约一致性：脚手架生成的 provider 应满足各 kind 的方法与返回结构。"""

    @pytest.mark.contract
    @pytest.mark.parametrize("kind", SUPPORTED_KINDS, ids=SUPPORTED_KINDS)
    def test_scaffold_generates_all_files(self, kind: str, tmp_path: Path) -> None:
        """脚手架应为每种 kind 生成完整文件集。"""
        root = ScaffoldGenerator().generate(f"test-{kind}-plugin", kind, tmp_path)
        expected = {"__init__.py", "provider.py", "manifest.yaml"}
        actual = {p.name for p in root.iterdir() if p.is_file()}
        assert expected <= actual
        assert (root / "tests" / "test_provider.py").is_file()

    @pytest.mark.contract
    @pytest.mark.parametrize("kind", SUPPORTED_KINDS, ids=SUPPORTED_KINDS)
    def test_manifest_category_matches_kind(self, kind: str, tmp_path: Path) -> None:
        """生成的 manifest.yaml 的 category 应与 KIND_CATEGORY_MAP 一致。"""
        root = ScaffoldGenerator().generate(f"test-{kind}-plugin", kind, tmp_path)
        import yaml

        raw = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
        assert raw["spec"]["category"] == KIND_CATEGORY_MAP[kind]

    @pytest.mark.contract
    @pytest.mark.parametrize("kind", SUPPORTED_KINDS, ids=SUPPORTED_KINDS)
    def test_provider_contract_methods(self, kind: str, tmp_path: Path) -> None:
        """脚手架生成的 provider 应满足 kind 对应的契约方法与返回结构。"""
        root = ScaffoldGenerator().generate(f"test-{kind}-plugin", kind, tmp_path)
        module = _load_provider_module(root)

        # 找到 provider 类：在当前模块内定义的非下划线类（排除导入的 typing.Any 等）
        cls = next(
            obj
            for name, obj in vars(module).items()
            if isinstance(obj, type)
            and not name.startswith("_")
            and getattr(obj, "__module__", "") == module.__name__
        )
        provider = cls()

        contract = _PROVIDER_CONTRACTS[kind]
        for method_name, required_keys in contract.items():
            method = getattr(provider, method_name)
            # 按方法名构造最小调用参数
            result = _invoke_contract_method(method_name, method)
            if method_name == "discover":
                assert isinstance(result, list)
            else:
                assert isinstance(result, dict), f"{kind}.{method_name} must return dict"
                for key in required_keys:
                    assert key in result, f"{kind}.{method_name} missing key {key!r}"


def _invoke_contract_method(method_name: str, method: Any) -> Any:
    """按方法名用最小参数调用契约方法。"""
    if method_name in ("evaluate", "record"):
        return method({})
    if method_name in ("execute", "apply"):
        return method({}, {})
    if method_name in ("start",):
        return method({})
    if method_name in ("discover",):
        return method()
    raise AssertionError(f"unknown contract method: {method_name}")


# ===========================================================================
# 3. 插件生命周期状态机
# ===========================================================================


class TestPluginLifecycleStateMachine:
    """插件生命周期状态机契约：合法转移通过、非法转移抛异常。"""

    @pytest.mark.contract
    def test_has_six_states(self) -> None:
        """生命周期应恰好为 6 态。"""
        assert len(PluginLifecycleState) == 6

    @pytest.mark.contract
    @pytest.mark.parametrize(
        ("cur", "to"),
        [
            (PluginLifecycleState.DISCOVERED, PluginLifecycleState.VALIDATING),
            (PluginLifecycleState.VALIDATING, PluginLifecycleState.INSTALLED),
            (PluginLifecycleState.VALIDATING, PluginLifecycleState.UNINSTALLED),
            (PluginLifecycleState.INSTALLED, PluginLifecycleState.ENABLED),
            (PluginLifecycleState.INSTALLED, PluginLifecycleState.DISABLED),
            (PluginLifecycleState.ENABLED, PluginLifecycleState.DISABLED),
            (PluginLifecycleState.DISABLED, PluginLifecycleState.ENABLED),
            (PluginLifecycleState.ENABLED, PluginLifecycleState.UNINSTALLED),
        ],
        ids=lambda v: v.value if isinstance(v, PluginLifecycleState) else str(v),
    )
    def test_legal_transition_allowed(
        self, cur: PluginLifecycleState, to: PluginLifecycleState
    ) -> None:
        """合法转移应通过校验。"""
        validate_plugin_transition(cur, to)  # 不抛异常即通过

    @pytest.mark.contract
    @pytest.mark.parametrize(
        ("cur", "to"),
        [
            (PluginLifecycleState.DISCOVERED, PluginLifecycleState.ENABLED),
            (PluginLifecycleState.DISCOVERED, PluginLifecycleState.INSTALLED),
            (PluginLifecycleState.ENABLED, PluginLifecycleState.VALIDATING),
            (PluginLifecycleState.UNINSTALLED, PluginLifecycleState.ENABLED),
            (PluginLifecycleState.UNINSTALLED, PluginLifecycleState.DISCOVERED),
        ],
        ids=lambda v: v.value if isinstance(v, PluginLifecycleState) else str(v),
    )
    def test_illegal_transition_rejected(
        self, cur: PluginLifecycleState, to: PluginLifecycleState
    ) -> None:
        """非法转移应抛 InvalidStateTransition。"""
        with pytest.raises(InvalidStateTransition):
            validate_plugin_transition(cur, to)

    @pytest.mark.contract
    def test_uninstalled_is_terminal(self) -> None:
        """UNINSTALLED 为终态，无任何出边。"""
        assert PLUGIN_TRANSITIONS[PluginLifecycleState.UNINSTALLED] == set()

    @pytest.mark.contract
    def test_string_state_accepted(self) -> None:
        """字符串形式的状态应被接受。"""
        validate_plugin_transition("Discovered", "Validating")  # 不抛异常

    @pytest.mark.contract
    def test_unknown_state_rejected(self) -> None:
        """未知状态字符串应抛异常。"""
        with pytest.raises(InvalidStateTransition):
            validate_plugin_transition("Discovered", "NotAState")


# ===========================================================================
# 4. 权限模型
# ===========================================================================


class TestPermissionModel:
    """权限模型契约：信任级别→隔离模式映射、UNTRUSTED 权限约束。"""

    @pytest.mark.contract
    def test_trusted_maps_to_in_process(self) -> None:
        """TRUSTED 应映射到 IN_PROCESS。"""
        guard = IsolationGuard()
        assert guard.get_mode(TrustLevel.TRUSTED) == IsolationMode.IN_PROCESS

    @pytest.mark.contract
    def test_verified_maps_to_restricted_rpc(self) -> None:
        """VERIFIED 应映射到 RESTRICTED_RPC。"""
        guard = IsolationGuard()
        assert guard.get_mode(TrustLevel.VERIFIED) == IsolationMode.RESTRICTED_RPC

    @pytest.mark.contract
    def test_untrusted_maps_to_restricted_rpc(self) -> None:
        """UNTRUSTED 应映射到 RESTRICTED_RPC。"""
        guard = IsolationGuard()
        assert guard.get_mode(TrustLevel.UNTRUSTED) == IsolationMode.RESTRICTED_RPC

    @pytest.mark.contract
    def test_untrusted_cannot_declare_database_permission(self) -> None:
        """UNTRUSTED 插件声明 database 权限应被拒绝。"""
        guard = IsolationGuard()
        with pytest.raises(IsolationError):
            guard.validate_manifest(TrustLevel.UNTRUSTED, ["database"])

    @pytest.mark.contract
    def test_untrusted_cannot_declare_admin_permission(self) -> None:
        """UNTRUSTED 插件声明 admin 权限应被拒绝。"""
        guard = IsolationGuard()
        with pytest.raises(IsolationError):
            guard.validate_manifest(TrustLevel.UNTRUSTED, ["admin"])

    @pytest.mark.contract
    def test_untrusted_can_declare_safe_permission(self) -> None:
        """UNTRUSTED 插件声明安全权限（如 metrics:read）应通过。"""
        guard = IsolationGuard()
        guard.validate_manifest(TrustLevel.UNTRUSTED, ["metrics:read"])  # 不抛异常

    @pytest.mark.contract
    def test_trusted_can_declare_any_permission(self) -> None:
        """TRUSTED 插件可声明任意权限。"""
        guard = IsolationGuard()
        guard.validate_manifest(TrustLevel.TRUSTED, ["database", "admin"])  # 不抛异常

    @pytest.mark.contract
    @pytest.mark.parametrize("resource", ["database", "filesystem", "network"])
    def test_restricted_rpc_blocks_direct_resources(self, resource: str) -> None:
        """RESTRICTED_RPC 模式应禁止直连 database/filesystem/network。"""
        guard = IsolationGuard()
        with pytest.raises(IsolationError):
            guard.check_access(TrustLevel.UNTRUSTED, resource)

    @pytest.mark.contract
    def test_in_process_allows_all_resources(self) -> None:
        """IN_PROCESS 模式应允许所有资源访问。"""
        guard = IsolationGuard()
        guard.check_access(TrustLevel.TRUSTED, "database")  # 不抛异常
