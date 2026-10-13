"""插件入口冒烟测试（进程内加载，CI architecture-docs job 调用）。

对 ``plugins/*/manifest.yaml`` 逐个执行：

1. 用 :class:`forgemesh.plugin.manifest.PluginManifest` 解析清单（含权限词表与入口点
   格式校验）；
2. ``spec.entrypoint`` 必须声明，并经 :func:`forgemesh.plugin.loader.load_entrypoint`
   按声明加载入口类：插件目录作为包加载，入口类必须定义在插件包内；
3. 基本健康检查：入口类构造器没有必填参数时实例化一次；需要宿主注入依赖（客户端、
   存储、配置等）时不实例化，只报告需要哪些参数。

逐插件输出 OK/FAIL 明细与汇总，任一插件失败即以退出码 1 结束。

加载前把 ``packages/*/src`` 前置注入 ``sys.path``（PEP 420 命名空间包，与根级
conftest.py 一致），使插件可导入 ``forgemesh.*`` 契约包。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

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


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _required_parameters(entry: type) -> list[str]:
    """入口类构造器中没有默认值的参数名。"""
    parameters = inspect.signature(entry).parameters.values()
    return [
        p.name
        for p in parameters
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]


def _check_plugin(plugin_dir: Path) -> str:
    """加载并检查一个插件，返回检查结论；任何问题都以异常报告。"""
    from forgemesh.plugin.loader import load_entrypoint
    from forgemesh.plugin.manifest import PluginManifest

    raw = yaml.safe_load((plugin_dir / "manifest.yaml").read_text(encoding="utf-8"))
    entrypoint = PluginManifest.model_validate(raw).spec.entrypoint
    if entrypoint is None:
        raise ValueError("spec.entrypoint is not declared")
    entry = load_entrypoint(plugin_dir, entrypoint)
    required = _required_parameters(entry)
    if required:
        return (
            f"{entrypoint} loaded; not instantiated, constructor needs host-provided "
            f"{', '.join(required)}"
        )
    entry()
    return f"{entrypoint} loaded and instantiated"


def main(plugins_dir: Path = PLUGINS_DIR) -> int:
    """入口：逐插件输出 OK/FAIL 明细，任一 FAIL 以退出码 1 结束。"""
    _inject_package_sources()
    plugin_dirs = sorted(
        d for d in plugins_dir.iterdir() if d.is_dir() and (d / "manifest.yaml").is_file()
    )
    if not plugin_dirs:
        print(f"ERROR: {_display(plugins_dir)}/ 下未找到任何插件 manifest", file=sys.stderr)
        return 1

    failed = 0
    for plugin_dir in plugin_dirs:
        try:
            outcome = _check_plugin(plugin_dir)
        except Exception as exc:
            print(f"FAIL: {_display(plugin_dir)}: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"OK: {_display(plugin_dir)}: {outcome}")

    print(f"summary: {len(plugin_dirs) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
