"""plugins 测试辅助：用 importlib 加载连字符目录下的 provider 模块。

插件目录使用连字符命名（``decision-rules`` / ``decision-jev`` / ``gpu-nvidia``），
无法作为常规 Python 包名导入。本 conftest 在 pytest 收集阶段通过 ``importlib``
从文件路径加载模块/包，并注册到 ``sys.modules``，使测试文件可直接
``from decision_rules_provider import ...`` 或 ``from gpu_nvidia_plugin.discovery import ...``
导入。

根级 ``conftest.py`` 已将 ``packages/*/src`` 注入 ``sys.path``，故
``forgemesh.decision.*`` / ``forgemesh.resource.*`` 在本 conftest 执行时已可导入。

兼容 Python 3.9+。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGINS_ROOT = Path(__file__).resolve().parents[3] / "plugins"


def _load_plugin_module(module_name: str, plugin_dir: str) -> None:
    """从 ``plugins/<plugin_dir>/provider.py`` 加载模块并注册到 sys.modules。

    Args:
        module_name: 注册到 ``sys.modules`` 的模块名（供测试 import 用）。
        plugin_dir: ``plugins/`` 下的插件目录名（允许连字符）。

    Raises:
        ImportError: 文件不存在或加载失败。
    """
    path = _PLUGINS_ROOT / plugin_dir / "provider.py"
    if not path.is_file():
        raise ImportError(f"plugin provider not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


def _load_plugin_package(package_name: str, plugin_dir: str) -> None:
    """把 ``plugins/<plugin_dir>/`` 作为 Python 包加载并注册到 sys.modules。

    通过设置 ``submodule_search_locations``，使子模块（如 ``discovery``）
    可通过 ``from <package_name>.discovery import ...`` 正常导入。

    Args:
        package_name: 注册到 ``sys.modules`` 的包名（供测试 import 用）。
        plugin_dir: ``plugins/`` 下的插件目录名（允许连字符）。

    Raises:
        ImportError: ``__init__.py`` 不存在或加载失败。
    """
    pkg_path = _PLUGINS_ROOT / plugin_dir
    init_path = pkg_path / "__init__.py"
    if not init_path.is_file():
        raise ImportError(f"plugin package __init__ not found: {init_path}")
    spec = importlib.util.spec_from_file_location(
        package_name,
        init_path,
        submodule_search_locations=[str(pkg_path)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {init_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)


# 预加载插件 provider 模块，供测试文件直接 import。
_load_plugin_module("decision_rules_provider", "decision-rules")
_load_plugin_module("decision_jev_provider", "decision-jev")
_load_plugin_module("workflow_reference_provider", "workflow-reference")

# 预加载 gpu-nvidia 插件包（含 discovery / preflight / health 子模块）。
_load_plugin_package("gpu_nvidia_plugin", "gpu-nvidia")
# 预加载 runtime-vllm 插件包（含 model / deploy / verify / release 子模块）。
_load_plugin_package("runtime_vllm_plugin", "runtime-vllm")
