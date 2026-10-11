"""插件 manifest 与 JSON Schema 校验（CI architecture-docs job 调用，WP-A2）。

两阶段校验（v1.1 第 15 章：从"文件存在/可解析"升级为真实校验）：

1. Schema 自校验：``api/schemas/*.json`` 必须是合法 JSON，且通过所选
   Validator 的 metaschema 检查（``check_schema``），即"可被 Draft 2020-12
   Validator 使用"；Validator 类按各 schema 自身 ``$schema`` 声明选择，
   未声明时缺省 Draft 2020-12。
2. 实例校验：用 ``api/schemas/plugin-manifest.schema.json`` 逐个校验
   ``plugins/*/manifest.yaml``（yaml.safe_load 后经 jsonschema 校验）。

schema 之外追加两条平台约定检查（manifest 必须满足而 schema 未强制）：

- ``spec.entrypoint`` 必填且为非空字符串（插件入口冒烟的依据）；
- ``metadata.version`` 必须是语义化版本 MAJOR.MINOR.PATCH（可选预发布/构建后缀）。

任一失败打印"文件名 + 校验错误路径"并以退出码 1 结束；
全部通过打印 ``OK: N manifests validated``。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "api" / "schemas"
PLUGINS_DIR = REPO_ROOT / "plugins"
MANIFEST_SCHEMA_PATH = SCHEMA_DIR / "plugin-manifest.schema.json"

# 语义化版本（简化 semver.org）：MAJOR.MINOR.PATCH[-prerelease][+build]。
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def _validator_cls(schema: dict) -> type:
    """按 schema 的 ``$schema`` 声明选择 Validator 类，未声明时缺省 Draft 2020-12。"""
    return validator_for(schema, default=Draft202012Validator)


def _self_validate_schemas() -> tuple[dict, list[str]]:
    """阶段一：``api/schemas/*.json`` 自校验（JSON 语法 + metaschema 可用性）。

    Returns:
        (manifest schema 文档, 错误列表)。错误列表非空时应中止实例校验阶段。
    """
    files = sorted(SCHEMA_DIR.glob("*.json"))
    if not files:
        return {}, ["api/schemas: 未找到任何 .json schema 文件"]

    errors: list[str] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{rel}: JSON 解析失败: {exc}")
            continue
        try:
            _validator_cls(doc).check_schema(doc)
        except SchemaError as exc:
            errors.append(f"{rel}: 不是合法 JSON Schema: {exc.message}")
    if errors:
        return {}, errors
    print(f"OK: {len(files)} JSON Schema(s) self-validated")

    try:
        manifest_schema = json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{MANIFEST_SCHEMA_PATH.relative_to(REPO_ROOT)}: 读取失败: {exc}"]
    return manifest_schema, []


def _platform_checks(rel: Path, instance: object) -> list[str]:
    """平台约定检查（schema 未覆盖）：entrypoint 必填非空、version 语义化版本。"""
    if not isinstance(instance, dict):
        return [f"{rel}: manifest 顶层不是映射"]

    errors: list[str] = []
    spec = instance.get("spec")
    if isinstance(spec, dict):
        entrypoint = spec.get("entrypoint")
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            errors.append(
                f"{rel}: $.spec.entrypoint: entrypoint 缺失或为空"
                "（平台约定：插件必须声明可执行入口）"
            )
    metadata = instance.get("metadata")
    if isinstance(metadata, dict):
        version = metadata.get("version")
        if not isinstance(version, str) or not SEMVER_RE.match(version):
            errors.append(
                f"{rel}: $.metadata.version: 版本格式非法"
                f"（应为 MAJOR.MINOR.PATCH 语义化版本）: {version!r}"
            )
    return errors


def _validate_manifests(manifest_schema: dict) -> tuple[int, list[str]]:
    """阶段二：用 plugin-manifest schema 逐个校验 ``plugins/*/manifest.yaml``。"""
    manifests = sorted(PLUGINS_DIR.glob("*/manifest.yaml"))
    if not manifests:
        return 0, ["plugins/: 未找到任何 */manifest.yaml"]

    validator = _validator_cls(manifest_schema)(manifest_schema)
    errors: list[str] = []
    for path in manifests:
        rel = path.relative_to(REPO_ROOT)
        try:
            instance = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{rel}: YAML 解析失败: {exc}")
            continue
        # 按错误路径稳定排序，输出"文件名 + json_path + 消息"。
        for verr in sorted(validator.iter_errors(instance), key=lambda e: str(e.json_path)):
            errors.append(f"{rel}: {verr.json_path}: {verr.message}")
        errors.extend(_platform_checks(rel, instance))
    return len(manifests), errors


def main() -> int:
    """入口：两阶段校验，失败打印全部错误并返回 1。"""
    manifest_schema, errors = _self_validate_schemas()
    manifest_count = 0
    if not errors:
        manifest_count, manifest_errors = _validate_manifests(manifest_schema)
        errors.extend(manifest_errors)

    if errors:
        print(f"FAIL: {len(errors)} 个校验错误", file=sys.stderr)
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(f"OK: {manifest_count} manifests validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
