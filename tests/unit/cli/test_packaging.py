"""CLI 包的依赖边界：CLI 是 API 的 HTTP 客户端，只导入标准库与 ``pyproject.toml`` 声明的
发行包，不导入控制平面或其他 ForgeMesh 包，安装它不需要服务端的代码。"""

from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

PYPROJECT = Path(__file__).resolve().parents[3] / "packages" / "cli" / "pyproject.toml"
#: 在新的解释器中导入 CLI 的入口，输出因此新导入的模块；``file`` 区分有文件的模块与
#: 扩展模块在运行时创建的模块（如 PyYAML 的 Cython 运行时 ``_cython_3_1_4``）。
_PROBE = """
import json, sys
before = set(sys.modules)
import forgemesh.cli.main
print(json.dumps([
    {"name": name, "file": getattr(sys.modules[name], "__file__", None) is not None}
    for name in sorted(set(sys.modules) - before)
]))
"""


def _imported() -> list[dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True, timeout=60
    )
    return json.loads(result.stdout)


def _normalize(distribution: str) -> str:
    return re.sub(r"[-_.]+", "-", distribution).lower()


def _project() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_the_cli_declares_exactly_the_distributions_it_imports() -> None:
    top_level = {module["name"].partition(".")[0] for module in _imported() if module["file"]}
    third_party = top_level - sys.stdlib_module_names - {"forgemesh"}
    owners = importlib.metadata.packages_distributions()
    imported = {_normalize(owner) for name in third_party for owner in owners.get(name, [name])}
    declared = {
        _normalize(re.match(r"[A-Za-z0-9._-]+", requirement).group())
        for requirement in _project()["dependencies"]
    }

    assert imported == declared == {"pyyaml"}


def test_the_cli_imports_no_other_forgemesh_package() -> None:
    names = [module["name"] for module in _imported()]

    assert "forgemesh.cli.main" in names
    assert [
        name
        for name in names
        if name.partition(".")[0] == "forgemesh"
        and name not in ("forgemesh", "forgemesh.cli")
        and not name.startswith("forgemesh.cli.")
    ] == []


def test_the_console_script_keeps_its_name() -> None:
    assert _project()["scripts"] == {"forgemesh": "forgemesh.cli.main:main"}
