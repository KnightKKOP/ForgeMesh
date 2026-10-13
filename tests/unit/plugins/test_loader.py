"""插件入口点加载单元测试（对齐 development-plan 5.7）。

覆盖：

* 仓库内全部插件都能按清单声明的入口点加载，入口类定义在各自插件包内；
* 清单入口点格式：拒绝 ``python -m ...``、裸类名等写法；
* 加载器：连字符目录、插件内相对导入、子包模块、同一目录只加载一次、同名目录互不冲突；
* 拒绝：缺 ``__init__.py``、模块不存在、入口点指向插件外的模块或类、属性不存在、不是类、
  插件代码导入失败（失败的插件包不留在 ``sys.modules``）；
* CI 冒烟脚本：全部插件通过时退出码 0，任一入口点损坏或构造失败时退出码 1。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from forgemesh.plugin.loader import (
    EntrypointError,
    load_entrypoint,
    load_plugin_package,
    plugin_package_name,
)
from forgemesh.plugin.manifest import PluginManifest, PluginSpec
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_DIRS = sorted(p.parent for p in (_REPO_ROOT / "plugins").glob("*/manifest.yaml"))
_ENTRY = "class Entry:\n    pass\n"


@pytest.fixture(autouse=True)
def _forget_loaded_plugins() -> Iterator[None]:
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("_forgemesh_plugin_"):
            del sys.modules[name]


def _write_plugin(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _manifest_yaml(entrypoint: str | None) -> str:
    spec: dict[str, object] = {"category": "policy", "permissions": ["resource.read"]}
    if entrypoint is not None:
        spec["entrypoint"] = entrypoint
    return yaml.safe_dump(
        {
            "apiVersion": "forgemesh.io/v1alpha1",
            "kind": "PluginManifest",
            "metadata": {"id": "forgemesh.policy.example", "version": "0.1.0"},
            "spec": spec,
        }
    )


class TestShippedPlugins:
    """仓库内插件按清单声明的入口点加载。"""

    def test_all_plugins_found(self) -> None:
        assert len(_PLUGIN_DIRS) == 10

    @pytest.mark.parametrize("plugin_dir", _PLUGIN_DIRS, ids=lambda p: p.name)
    def test_declared_entrypoint_loads(self, plugin_dir: Path) -> None:
        raw = yaml.safe_load((plugin_dir / "manifest.yaml").read_text(encoding="utf-8"))
        entrypoint = PluginManifest.model_validate(raw).spec.entrypoint
        assert entrypoint is not None

        entry = load_entrypoint(plugin_dir, entrypoint)

        module_path, class_name = entrypoint.split(":")
        assert entry.__name__ == class_name
        assert entry.__module__ == f"{plugin_package_name(plugin_dir)}.{module_path}"


class TestManifestEntrypointFormat:
    """清单 ``spec.entrypoint`` 的格式校验。"""

    @pytest.mark.parametrize(
        "entrypoint", ["provider:RulesProvider", "impl.core:Entry", "_private:_Entry"]
    )
    def test_module_colon_class_accepted(self, entrypoint: str) -> None:
        assert PluginSpec(category="policy", entrypoint=entrypoint).entrypoint == entrypoint

    @pytest.mark.parametrize(
        "entrypoint",
        [
            "python -m forgemesh_decision_rules",
            "RulesProvider",
            "provider.RulesProvider",
            "provider:Rules-Provider",
            "plugins/provider:Entry",
            "provider:RulesProvider\n",
            "",
        ],
    )
    def test_other_forms_rejected(self, entrypoint: str) -> None:
        with pytest.raises(ValidationError, match="entrypoint must be '<module>:<Class>'"):
            PluginSpec(category="policy", entrypoint=entrypoint)

    def test_entrypoint_optional_in_model(self) -> None:
        assert PluginSpec(category="policy").entrypoint is None


class TestLoadEntrypoint:
    """加载器的正常路径。"""

    def test_hyphenated_directory_with_relative_import(self, tmp_path: Path) -> None:
        plugin = _write_plugin(
            tmp_path / "my-plugin",
            {
                "__init__.py": "",
                "helpers.py": "VALUE = 42\n",
                "provider.py": "from .helpers import VALUE\n\n\nclass Entry:\n    value = VALUE\n",
            },
        )

        assert load_entrypoint(plugin, "provider:Entry").value == 42

    def test_module_in_subpackage(self, tmp_path: Path) -> None:
        plugin = _write_plugin(
            tmp_path / "plugin",
            {"__init__.py": "", "impl/__init__.py": "", "impl/core.py": _ENTRY},
        )

        assert load_entrypoint(plugin, "impl.core:Entry").__name__ == "Entry"

    def test_same_directory_loaded_once(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path / "plugin", {"__init__.py": "", "provider.py": _ENTRY})

        first = load_entrypoint(plugin, "provider:Entry")

        assert load_entrypoint(plugin, "provider:Entry") is first
        assert load_plugin_package(plugin) is sys.modules[plugin_package_name(plugin)]

    def test_directories_with_same_name_do_not_collide(self, tmp_path: Path) -> None:
        first = _write_plugin(
            tmp_path / "a" / "plugin",
            {"__init__.py": "", "provider.py": "class Entry:\n    origin = 'a'\n"},
        )
        second = _write_plugin(
            tmp_path / "b" / "plugin",
            {"__init__.py": "", "provider.py": "class Entry:\n    origin = 'b'\n"},
        )

        assert plugin_package_name(first) != plugin_package_name(second)
        assert load_entrypoint(first, "provider:Entry").origin == "a"
        assert load_entrypoint(second, "provider:Entry").origin == "b"


class TestLoadEntrypointRejected:
    """加载器拒绝的情形。"""

    @pytest.fixture
    def plugin(self, tmp_path: Path) -> Path:
        return _write_plugin(
            tmp_path / "plugin",
            {
                "__init__.py": "",
                "provider.py": (
                    "from collections import OrderedDict\n\n\n"
                    "def factory():\n    return None\n\n\n" + _ENTRY
                ),
            },
        )

    @pytest.mark.parametrize(
        "entrypoint", ["python -m forgemesh_x", "Entry", "provider:", "../provider:Entry"]
    )
    def test_invalid_format(self, plugin: Path, entrypoint: str) -> None:
        with pytest.raises(EntrypointError, match="invalid entrypoint"):
            load_entrypoint(plugin, entrypoint)

    def test_directory_without_init(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path / "plugin", {"provider.py": _ENTRY})

        with pytest.raises(EntrypointError, match=r"not a Python package \(no __init__.py\)"):
            load_entrypoint(plugin, "provider:Entry")

    def test_missing_module(self, plugin: Path) -> None:
        with pytest.raises(EntrypointError, match="has no module 'missing'"):
            load_entrypoint(plugin, "missing:Entry")

    def test_module_outside_plugin_not_reachable(self, plugin: Path) -> None:
        """模块路径只在插件包内解析，``os:system`` 不会解析到标准库 os。"""
        with pytest.raises(EntrypointError, match="has no module 'os'"):
            load_entrypoint(plugin, "os:system")

    def test_class_defined_outside_plugin(self, plugin: Path) -> None:
        with pytest.raises(EntrypointError, match="collections.OrderedDict, which is not defined"):
            load_entrypoint(plugin, "provider:OrderedDict")

    def test_missing_attribute(self, plugin: Path) -> None:
        with pytest.raises(EntrypointError, match="has no 'Missing'"):
            load_entrypoint(plugin, "provider:Missing")

    def test_not_a_class(self, plugin: Path) -> None:
        with pytest.raises(EntrypointError, match="is not a class"):
            load_entrypoint(plugin, "provider:factory")

    def test_missing_dependency_of_plugin_code(self, tmp_path: Path) -> None:
        plugin = _write_plugin(
            tmp_path / "plugin",
            {"__init__.py": "", "provider.py": "import forgemesh_no_such_dependency\n" + _ENTRY},
        )

        with pytest.raises(
            EntrypointError, match="cannot import module 'provider'.*forgemesh_no_such_dependency"
        ) as excinfo:
            load_entrypoint(plugin, "provider:Entry")
        assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)

    def test_error_raised_by_plugin_module(self, tmp_path: Path) -> None:
        plugin = _write_plugin(
            tmp_path / "plugin", {"__init__.py": "", "provider.py": "raise LookupError('boom')\n"}
        )

        with pytest.raises(EntrypointError, match="LookupError: boom"):
            load_entrypoint(plugin, "provider:Entry")

    def test_failed_package_not_cached(self, tmp_path: Path) -> None:
        plugin = _write_plugin(
            tmp_path / "plugin",
            {"__init__.py": "raise ValueError('broken init')\n", "provider.py": _ENTRY},
        )

        with pytest.raises(EntrypointError, match="cannot import plugin package.*broken init"):
            load_entrypoint(plugin, "provider:Entry")
        assert plugin_package_name(plugin) not in sys.modules

        (plugin / "__init__.py").write_text("", encoding="utf-8")
        assert load_entrypoint(plugin, "provider:Entry").__name__ == "Entry"


def _smoke_script() -> ModuleType:
    path = _REPO_ROOT / "scripts" / "plugin_entrypoint_smoke.py"
    spec = importlib.util.spec_from_file_location("_plugin_entrypoint_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEntrypointSmokeScript:
    """``scripts/plugin_entrypoint_smoke.py``（CI 插件入口冒烟）。"""

    @pytest.fixture(autouse=True)
    def _restore_sys_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "path", list(sys.path))

    def test_shipped_plugins_pass(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _smoke_script().main() == 0

        out = capsys.readouterr().out
        assert "summary: 10 passed, 0 failed" in out
        assert "OK: plugins/decision-rules: provider:RulesProvider loaded and instantiated" in out
        assert "needs host-provided client" in out

    def test_broken_or_missing_entrypoint_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for name, entrypoint in (("good", "provider:Entry"), ("broken", "provider:Missing")):
            _write_plugin(
                tmp_path / name,
                {
                    "__init__.py": "",
                    "provider.py": _ENTRY,
                    "manifest.yaml": _manifest_yaml(entrypoint),
                },
            )
        _write_plugin(
            tmp_path / "unset", {"__init__.py": "", "manifest.yaml": _manifest_yaml(None)}
        )

        assert _smoke_script().main(tmp_path) == 1

        out = capsys.readouterr().out
        assert f"OK: {tmp_path / 'good'}: provider:Entry loaded and instantiated" in out
        assert f"FAIL: {tmp_path / 'broken'}: EntrypointError:" in out
        assert f"FAIL: {tmp_path / 'unset'}: ValueError: spec.entrypoint is not declared" in out
        assert "summary: 1 passed, 2 failed" in out

    def test_constructor_failure_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_plugin(
            tmp_path / "plugin",
            {
                "__init__.py": "",
                "provider.py": (
                    "class Entry:\n    def __init__(self):\n        raise LookupError('no backend')\n"
                ),
                "manifest.yaml": _manifest_yaml("provider:Entry"),
            },
        )

        assert _smoke_script().main(tmp_path) == 1
        assert "LookupError: no backend" in capsys.readouterr().out

    def test_host_dependencies_not_instantiated(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_plugin(
            tmp_path / "plugin",
            {
                "__init__.py": "",
                "provider.py": (
                    "class Entry:\n    def __init__(self, store, *, clock=None):\n"
                    "        raise AssertionError('must not be constructed')\n"
                ),
                "manifest.yaml": _manifest_yaml("provider:Entry"),
            },
        )

        assert _smoke_script().main(tmp_path) == 0
        assert "not instantiated, constructor needs host-provided store" in capsys.readouterr().out
