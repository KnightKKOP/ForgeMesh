"""ForgeMesh 根级 pytest 配置。

目的：在子包尚未通过 ``pip install -e`` 安装时，让 pytest 仍能导入
``forgemesh.*`` 命名空间包（PEP 420）。

做法：以 ``pathlib`` 动态发现 ``<repo_root>/packages/*/src`` 目录，并将
其绝对路径前置注入 ``sys.path``。该注入在 conftest 模块导入时立即生效，
早于任何测试模块的收集与导入。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_PACKAGES_DIR = _REPO_ROOT / "packages"
_SRC_DIRNAME = "src"


def _discover_src_dirs(packages_dir: Path) -> list:
    """发现 ``packages/<pkg>/src`` 目录，返回排序去重后的 Path 列表。

    若 ``packages`` 目录不存在（例如尚未展开骨架），返回空列表。
    """
    if not packages_dir.is_dir():
        return []

    discovered = []
    for package_dir in sorted(packages_dir.iterdir()):
        src_dir = package_dir / _SRC_DIRNAME
        if src_dir.is_dir():
            discovered.append(src_dir.resolve())
    return discovered


def _inject_sys_path(paths: list) -> None:
    """将给定路径加入 ``sys.path``，保持顺序且避免重复。"""
    for path in reversed(paths):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


# 模块导入即生效：确保 pytest 收集阶段前命名空间包可被导入。
_inject_sys_path(_discover_src_dirs(_PACKAGES_DIR))
