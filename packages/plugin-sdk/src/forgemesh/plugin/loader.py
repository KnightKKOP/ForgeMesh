"""按清单 ``spec.entrypoint`` 进程内加载插件入口类（对齐 development-plan 5.7）。

入口点形如 ``<模块>:<类>``（如 ``provider:RulesProvider``、``discovery:GPUDiscoverer``），
模块路径相对于插件包：插件目录必须含 ``__init__.py``，目录名可以带连字符。插件包以按
目录绝对路径派生的私有名称注册到 ``sys.modules``，同一目录只执行一次，不同目录互不冲突。

入口点只能指向插件包内定义的类，不能借此导入插件目录之外的模块或对象。加载会在当前
进程中执行插件代码，本模块不提供任何隔离。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

from forgemesh.plugin.manifest import ENTRYPOINT_PATTERN

try:  # 优先复用核心包统一异常体系
    from forgemesh.core.errors import ForgeMeshError
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class ForgeMeshError(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的统一异常基类。"""


class EntrypointError(ForgeMeshError):
    """入口点格式非法，或无法从插件包加载入口类。"""


def plugin_package_name(plugin_dir: str | Path) -> str:
    """返回插件目录在 ``sys.modules`` 中注册的包名。"""
    resolved = Path(plugin_dir).resolve()
    slug = re.sub(r"\W", "_", resolved.name, flags=re.ASCII)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"_forgemesh_plugin_{slug}_{digest}"


def load_plugin_package(plugin_dir: str | Path) -> ModuleType:
    """把插件目录作为 Python 包加载（已加载时直接返回）。

    Raises:
        EntrypointError: 目录缺少 ``__init__.py``，或执行 ``__init__.py`` 失败。
    """
    root = Path(plugin_dir).resolve()
    name = plugin_package_name(root)
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    init = root / "__init__.py"
    if not init.is_file():
        raise EntrypointError(
            f"plugin directory {plugin_dir} is not a Python package (no __init__.py)"
        )
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise EntrypointError(f"cannot create an import spec for plugin {plugin_dir}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    try:
        spec.loader.exec_module(package)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise EntrypointError(
            f"cannot import plugin package {plugin_dir}: {type(exc).__name__}: {exc}"
        ) from exc
    return package


def load_entrypoint(plugin_dir: str | Path, entrypoint: str) -> type:
    """按入口点 ``<模块>:<类>`` 从插件包加载入口类。

    Args:
        plugin_dir: 插件目录（含 ``__init__.py`` 与 ``manifest.yaml``）。
        entrypoint: 清单 ``spec.entrypoint``。

    Returns:
        插件包内定义的入口类（未实例化）。

    Raises:
        EntrypointError: 入口点格式非法、模块无法导入、属性不存在、不是类，或类定义在
            插件包之外。
    """
    if re.fullmatch(ENTRYPOINT_PATTERN, entrypoint) is None:
        raise EntrypointError(
            f"invalid entrypoint {entrypoint!r}: expected '<module>:<Class>' relative to the "
            "plugin package"
        )
    module_path, attr = entrypoint.split(":")
    package = load_plugin_package(plugin_dir)
    qualified = f"{package.__name__}.{module_path}"
    try:
        module = importlib.import_module(qualified)
    except ModuleNotFoundError as exc:
        # 缺失的是入口模块自身（或其上级包），而不是插件代码依赖的其他模块。
        if exc.name is not None and f"{qualified}.".startswith(f"{exc.name}."):
            raise EntrypointError(f"plugin {plugin_dir} has no module {module_path!r}") from exc
        raise EntrypointError(
            f"cannot import module {module_path!r} of plugin {plugin_dir}: {exc}"
        ) from exc
    except Exception as exc:
        raise EntrypointError(
            f"cannot import module {module_path!r} of plugin {plugin_dir}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        entry = getattr(module, attr)
    except AttributeError as exc:
        raise EntrypointError(
            f"module {module_path!r} of plugin {plugin_dir} has no {attr!r}"
        ) from exc
    if not isinstance(entry, type):
        raise EntrypointError(f"entrypoint {entrypoint!r} of plugin {plugin_dir} is not a class")
    if not f"{entry.__module__}.".startswith(f"{package.__name__}."):
        raise EntrypointError(
            f"entrypoint {entrypoint!r} of plugin {plugin_dir} names "
            f"{entry.__module__}.{entry.__qualname__}, which is not defined in the plugin package"
        )
    return entry


__all__ = [
    "EntrypointError",
    "load_entrypoint",
    "load_plugin_package",
    "plugin_package_name",
]
