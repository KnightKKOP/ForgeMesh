"""``scripts/check_package_imports.py``（CI 全新虚拟环境导入检查）。

构建与安装需要访问包索引，由 CI 的 package-imports job 端到端执行；这里覆盖不需要
网络的部分：依赖闭包、源码树中的模块、导入探针与结果对照。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import venv
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = _REPO_ROOT / "scripts" / "check_package_imports.py"
    spec = importlib.util.spec_from_file_location("_check_package_imports", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在处理字符串注解时要从 sys.modules 中找到定义它的模块。
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _write_package(
    packages_dir: Path,
    directory: str,
    name: str,
    dependencies: list[str],
    extras: dict[str, list[str]] | None = None,
    root: str = "demo",
) -> Path:
    package = packages_dir / directory
    (package / "src" / "forgemesh" / root).mkdir(parents=True)
    (package / "src" / "forgemesh" / root / "__init__.py").write_text("", encoding="utf-8")
    lines = ["[project]", f'name = "{name}"', f"dependencies = {dependencies!r}"]
    if extras:
        lines.append("[project.optional-dependencies]")
        lines.extend(f"{extra} = {requirements!r}" for extra, requirements in extras.items())
    (package / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return package


class TestResolveLocal:
    def test_repository_packages_resolve_within_packages_dir(self, script: ModuleType) -> None:
        packages = script.load_packages()

        assert len(packages) == 10
        for name, package in packages.items():
            assert len(package.roots) == 1, name
            assert name in script.resolve_local(name, packages)
        control_plane = script.resolve_local("forgemesh-control-plane", packages)
        assert control_plane["forgemesh-control-plane"] == {"api"}
        assert control_plane["forgemesh-core"] == set()
        assert script.resolve_local("forgemesh-cli", packages) == {"forgemesh-cli": set()}

    def test_follows_dependencies_and_extras(self, script: ModuleType, tmp_path: Path) -> None:
        _write_package(
            tmp_path,
            "a",
            "forgemesh-a",
            ["pydantic>=2,<3", "ForgeMesh_B[extra1] >= 0.0.1"],
            extras={"cli": ["forgemesh-c; python_version >= '3.12'"]},
        )
        _write_package(tmp_path, "b", "forgemesh-b", [], extras={"extra1": ["forgemesh.d"]})
        _write_package(tmp_path, "c", "forgemesh-c", [])
        _write_package(tmp_path, "d", "forgemesh-d", ["forgemesh-b"])
        _write_package(tmp_path, "e", "forgemesh-e", [])

        packages = script.load_packages(tmp_path)

        assert script.resolve_local("forgemesh-a", packages) == {
            "forgemesh-a": {"cli"},
            "forgemesh-b": {"extra1"},
            "forgemesh-c": set(),
            "forgemesh-d": set(),
        }

    def test_rejects_forgemesh_dependency_outside_packages_dir(
        self, script: ModuleType, tmp_path: Path
    ) -> None:
        _write_package(tmp_path, "a", "forgemesh-a", ["forgemesh-missing>=1"])

        with pytest.raises(ValueError, match="forgemesh-missing"):
            script.resolve_local("forgemesh-a", script.load_packages(tmp_path))


def test_source_modules_include_namespace_root_and_skip_main(
    script: ModuleType, tmp_path: Path
) -> None:
    root = tmp_path / "tool" / "src" / "forgemesh" / "tool"
    (root / "sub").mkdir(parents=True)
    for relative in ("main.py", "__main__.py", "sub/__init__.py", "sub/leaf.py"):
        (root / relative).write_text("", encoding="utf-8")
    package = script.LocalPackage(
        name="forgemesh-tool", path=tmp_path / "tool", dependencies=[], roots=["forgemesh.tool"]
    )

    assert script.source_modules(package) == {
        "forgemesh.tool",
        "forgemesh.tool.main",
        "forgemesh.tool.sub",
        "forgemesh.tool.sub.leaf",
    }


def test_probe_reports_undeclared_import_in_isolated_interpreter(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(env_dir)
    python = env_dir / "bin" / "python"
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    demo = Path(purelib) / "forgemesh" / "demo"
    (demo / "sub").mkdir(parents=True)
    modules = {
        "__init__.py": "",
        "good.py": "VALUE = 1\n",
        "bad.py": "import undeclared_dependency\n",
        "__main__.py": "raise SystemExit('__main__ must not be imported')\n",
        "sub/__init__.py": "print('noise on stdout')\n",
        "sub/leaf.py": "from forgemesh.demo import good\n",
    }
    for relative, source in modules.items():
        (demo / relative).write_text(source, encoding="utf-8")
    # 依赖出现在 PYTHONPATH 或工作目录中也不算数：探针以 -I 运行。
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "undeclared_dependency.py").write_text("", encoding="utf-8")
    (env_dir / "undeclared_dependency.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(shadow))

    result = script.probe(python, ["forgemesh.demo"], env_dir)

    assert result.failures == {
        "forgemesh.demo.bad": "ModuleNotFoundError: No module named 'undeclared_dependency'"
    }
    assert result.imported == {
        "forgemesh.demo",
        "forgemesh.demo.good",
        "forgemesh.demo.sub",
        "forgemesh.demo.sub.leaf",
    }


class TestCompare:
    @pytest.fixture
    def package(self, script: ModuleType, tmp_path: Path) -> Any:
        root = tmp_path / "demo" / "src" / "forgemesh" / "demo"
        root.mkdir(parents=True)
        for name in ("__init__.py", "a.py", "b.py"):
            (root / name).write_text("", encoding="utf-8")
        return script.LocalPackage(
            name="forgemesh-demo", path=tmp_path / "demo", dependencies=[], roots=["forgemesh.demo"]
        )

    def test_matching_modules_pass(self, script: ModuleType, package: Any) -> None:
        imported = {"forgemesh.demo", "forgemesh.demo.a", "forgemesh.demo.b"}

        assert script.compare(package, script.ProbeResult(imported=imported, failures={})) == []

    def test_import_failures_are_reported_alone(self, script: ModuleType, package: Any) -> None:
        result = script.ProbeResult(
            imported={"forgemesh.demo"},
            failures={"forgemesh.demo.b": "ModuleNotFoundError: No module named 'yaml'"},
        )

        assert script.compare(package, result) == [
            "forgemesh.demo.b: ModuleNotFoundError: No module named 'yaml'"
        ]

    def test_missing_and_unexpected_modules_fail(self, script: ModuleType, package: Any) -> None:
        result = script.ProbeResult(
            imported={"forgemesh.demo", "forgemesh.demo.a", "forgemesh.demo.stale"}, failures={}
        )

        errors = script.compare(package, result)

        assert len(errors) == 2
        assert errors[0].startswith("forgemesh.demo.b: ")
        assert errors[1].startswith("forgemesh.demo.stale: ")


def test_main_rejects_unknown_package_directory(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["core-contracts", "no-such-package"]) == 1

    assert "no-such-package" in capsys.readouterr().err
