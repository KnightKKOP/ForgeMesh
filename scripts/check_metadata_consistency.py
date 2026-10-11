"""packages/*/pyproject.toml 元数据一致性检查（CI WP-A2）。

检查项（v1.1 信任基线）：

- ``requires-python`` 必须精确等于 ``">=3.12"``：v1.1 统一 Python 3.12 基线；
- ``description`` 不得含 ``"skeleton"`` / ``"placeholder"``（大小写不敏感）：
  v1.1 起包元数据不得保留骨架/占位描述。

注：``requires-python`` 由元数据统一任务（另一工作项）改为 ``">=3.12"``，
本脚本按目标值检查；在该任务落地前运行会失败，属预期门槛——错误信息
会逐包列出当前值与期望值，便于定位遗漏的包。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
EXPECTED_REQUIRES_PYTHON = ">=3.12"
FORBIDDEN_DESCRIPTION_WORDS = ("skeleton", "placeholder")


def _check_pyproject(path: Path) -> list[str]:
    """校验单个 pyproject.toml，返回错误列表（空列表表示通过）。"""
    rel = path.relative_to(REPO_ROOT)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"{rel}: TOML 解析失败: {exc}"]

    project: Any = data.get("project")
    if not isinstance(project, dict):
        return [f"{rel}: 缺少 [project] 表"]

    name = project.get("name")
    if not isinstance(name, str) or not name:
        name = path.parent.name

    errors: list[str] = []
    requires_python = project.get("requires-python")
    if requires_python != EXPECTED_REQUIRES_PYTHON:
        errors.append(
            f"{rel} ({name}): requires-python={requires_python!r}, "
            f"必须精确等于 {EXPECTED_REQUIRES_PYTHON!r}"
        )

    description = project.get("description", "")
    lowered = description.lower() if isinstance(description, str) else ""
    hit = [word for word in FORBIDDEN_DESCRIPTION_WORDS if word in lowered]
    if hit:
        errors.append(f"{rel} ({name}): description 含禁用词 {hit}: {description!r}")
    return errors


def main() -> int:
    """入口：遍历全部包，任一违规列出包名与字段并以退出码 1 结束。"""
    pyprojects = sorted(PACKAGES_DIR.glob("*/pyproject.toml"))
    if not pyprojects:
        print(f"ERROR: {PACKAGES_DIR.relative_to(REPO_ROOT)}/ 下未找到任何 pyproject.toml",
              file=sys.stderr)
        return 1

    errors: list[str] = []
    for path in pyprojects:
        errors.extend(_check_pyproject(path))

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        print(f"FAIL: {len(errors)} 处元数据不一致", file=sys.stderr)
        return 1
    print(f"OK: {len(pyprojects)} 个包元数据一致 (requires-python={EXPECTED_REQUIRES_PYTHON!r}, "
          "无 skeleton/placeholder 描述)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
