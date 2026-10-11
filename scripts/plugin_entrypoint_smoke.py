"""插件入口冒烟测试（进程内 importlib 加载，CI WP-A2）。

加载机制与 ``tests/unit/plugins/conftest.py`` 同款：

- 遍历 ``plugins/*/manifest.yaml``，读取 ``spec.entrypoint``（缺失则 SKIP 计数）；
- 目录含 ``provider.py`` 且无相对导入（``from .`` / ``import .``）→ 按文件路径加载
  provider.py；
- 否则含 ``__init__.py`` → 作为包加载（``submodule_search_locations`` 指向插件目录，
  使 ``from .submodule import ...`` 相对导入可用，如 metering-basic 的 provider.py
  依赖 ``.usage``）；
- 断言模块可加载、且存在至少一个公有类（``inspect.isclass`` 且不以下划线开头）。

加载前把 ``packages/*/src`` 前置注入 ``sys.path``（PEP 420 命名空间包，
与根级 conftest.py 一致），使插件可导入 ``forgemesh.*`` 契约包。
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"
PACKAGES_DIR = REPO_ROOT / "packages"


def _inject_package_sources() -> None:
    """把 ``packages/*/src`` 前置注入 sys.path（去重），供 forgemesh.* 导入。"""
    if not PACKAGES_DIR.is_dir():
        return
    for pkg in sorted(PACKAGES_DIR.iterdir()):
        src = pkg / "src"
        if pkg.is_dir() and src.is_dir():
            resolved = str(src.resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)


RELATIVE_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s*\.", re.MULTILINE)


def _load_plugin(plugin_dir: Path, module_name: str) -> Any:
    """按 conftest 同款机制加载插件主模块。

    策略：provider.py 无相对导入时按单文件加载；含相对导入（如
    metering-basic 依赖 ``.usage``）或无 provider.py 时按包加载。

    Raises:
        ImportError: 目录无可加载入口，或加载过程失败。
    """
    provider = plugin_dir / "provider.py"
    init = plugin_dir / "__init__.py"
    if provider.is_file() and not RELATIVE_IMPORT_RE.search(provider.read_text(encoding="utf-8")):
        spec = importlib.util.spec_from_file_location(module_name, provider)
    elif init.is_file():
        spec = importlib.util.spec_from_file_location(
            module_name, init, submodule_search_locations=[str(plugin_dir)]
        )
    else:
        raise ImportError(f"插件目录无可加载入口（provider.py/__init__.py）: {plugin_dir}")
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为插件创建导入 spec: {plugin_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _public_classes(module: Any) -> list[str]:
    """返回模块内可见的公有类名。

    优先列出本模块（含其子模块）定义的类；纯 re-export 的包 ``__init__``
    回退为列出全部公有类（满足"存在任一公有类"的冒烟断言）。
    """
    all_public = sorted(
        name
        for name, obj in vars(module).items()
        if inspect.isclass(obj) and not name.startswith("_")
    )
    own = [
        name
        for name in all_public
        if str(getattr(vars(module)[name], "__module__", "")).startswith(module.__name__)
    ]
    return own or all_public


def main() -> int:
    """入口：逐插件输出 OK/FAIL/SKIP 明细，任一 FAIL 以退出码 1 结束。"""
    _inject_package_sources()
    plugin_dirs = sorted(
        d for d in PLUGINS_DIR.iterdir() if d.is_dir() and (d / "manifest.yaml").is_file()
    )
    if not plugin_dirs:
        print(f"ERROR: {PLUGINS_DIR.relative_to(REPO_ROOT)}/ 下未找到任何插件 manifest",
              file=sys.stderr)
        return 1

    failed = 0
    skipped = 0
    for plugin_dir in plugin_dirs:
        rel = plugin_dir.relative_to(REPO_ROOT)
        try:
            manifest = yaml.safe_load((plugin_dir / "manifest.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            print(f"FAIL: {rel}: manifest 读取失败: {exc}")
            failed += 1
            continue

        entrypoint = ""
        if isinstance(manifest, dict):
            spec = manifest.get("spec")
            if isinstance(spec, dict) and isinstance(spec.get("entrypoint"), str):
                entrypoint = spec["entrypoint"].strip()
        if not entrypoint:
            print(f"SKIP: {rel}: spec.entrypoint 缺失，跳过冒烟")
            skipped += 1
            continue

        module_name = plugin_dir.name.replace("-", "_") + "_smoke"
        try:
            module = _load_plugin(plugin_dir, module_name)
            classes = _public_classes(module)
            if not classes:
                raise ImportError("模块加载成功但未定义任何公有类")
        except Exception as exc:
            print(f"FAIL: {rel} (entrypoint={entrypoint!r}): {type(exc).__name__}: {exc}")
            failed += 1
            continue
        shown = ", ".join(classes[:5]) + ("..." if len(classes) > 5 else "")
        print(f"OK: {rel} (entrypoint={entrypoint!r}): 已加载 {module_name}，公有类: {shown}")

    passed = len(plugin_dirs) - failed - skipped
    print(f"summary: {passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
