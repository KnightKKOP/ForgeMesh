"""plugins 测试辅助：用 importlib 加载连字符目录下的 provider 模块。

插件目录使用连字符命名（``decision-rules`` / ``decision-jev``），无法作为
常规 Python 包名导入。本 conftest 在 pytest 收集阶段通过 ``importlib`` 从
文件路径加载 ``provider.py``，并注册到 ``sys.modules``，使测试文件可直接
``from decision_rules_provider import ...`` 导入。

根级 ``conftest.py`` 已将 ``packages/*/src`` 注入 ``sys.path``，故
``forgemesh.decision.*`` 在本 conftest 执行时已可导入。

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


# 预加载插件 provider 模块，供测试文件直接 import。
_load_plugin_module("decision_rules_provider", "decision-rules")
_load_plugin_module("decision_jev_provider", "decision-jev")
_load_plugin_module("workflow_reference_provider", "workflow-reference")
