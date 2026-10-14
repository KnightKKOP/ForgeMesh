"""OpenAPI 规格语义校验（纯标准库 + PyYAML，CI WP-A2）。

校验项：

1. 每个 operation 必须有 ``operationId``，且全局唯一；
2. 文档内全部本地 ``$ref`` 必须可解析（递归扫描 ``components`` 与 ``paths``，
   覆盖 components/schemas、components/responses 及请求/响应体等嵌套位置）；
3. 每个 operation 至少声明一个 2xx 成功响应与一个 4xx 错误响应；
4. 每个 response 必须含 ``$ref`` 或 ``content``（204/304 无正文语义豁免）。

设计说明（探针豁免）：``/healthz``、``/readyz`` 等 liveness/readiness 探针由
基础设施直连调用、无认证与错误响应语义（本规格中 /healthz 仅 200，/readyz
未就绪为 503），且 openapi.yaml 不在 CI 补强任务的修改范围，故对 tags 含
``probes`` 的 operation 豁免 4xx 要求（2xx 要求不豁免）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "api" / "openapi" / "openapi.yaml"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
# 无响应正文语义的状态码，豁免 $ref/content 检查。
NO_BODY_STATUS = {"204", "304"}
PROBE_TAG = "probes"


def _check_ref(ref: str, doc: dict, where: str, errors: list[str]) -> None:
    """校验单个本地 JSON Pointer $ref 能否从 doc 解析到具体组件。"""
    if not ref.startswith("#"):
        errors.append(f"{where}: 不支持的外部 $ref: {ref!r}")
        return
    pointer = ref[1:]
    if pointer in ("", "/"):
        return
    cur: Any = doc
    for seg in pointer.lstrip("/").split("/"):
        seg = seg.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or seg not in cur:
            errors.append(f"{where}: $ref {ref!r} 无法解析（组件 {seg!r} 不存在）")
            return
        cur = cur[seg]


def _walk_refs(node: Any, doc: dict, where: str, errors: list[str]) -> None:
    """递归扫描 node（dict/list）内所有 $ref 并逐一校验。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                _check_ref(value, doc, where, errors)
            else:
                _walk_refs(value, doc, f"{where}.{key}", errors)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _walk_refs(item, doc, f"{where}[{idx}]", errors)


def _check_responses(op: dict, where: str, errors: list[str]) -> None:
    """校验单个 operation 的 responses：状态码族完备性与响应体完备性。"""
    responses = op.get("responses")
    if not isinstance(responses, dict) or not responses:
        errors.append(f"{where}.responses: 缺失或为空")
        return

    codes = {str(code) for code in responses}
    has_2xx = any(len(code) == 3 and code.startswith("2") for code in codes)
    has_4xx = any(len(code) == 3 and code.startswith("4") for code in codes)
    if not has_2xx:
        errors.append(f"{where}.responses: 缺少 2xx 成功响应（现有: {sorted(codes)}）")
    # 探针（liveness/readiness）无认证与错误响应语义，豁免 4xx 要求，见模块 docstring。
    if not has_4xx and PROBE_TAG not in (op.get("tags") or []):
        errors.append(f"{where}.responses: 缺少 4xx 错误响应（现有: {sorted(codes)}）")

    for code, resp in responses.items():
        code_str = str(code)
        if code_str in NO_BODY_STATUS:
            continue
        if not isinstance(resp, dict) or ("$ref" not in resp and "content" not in resp):
            errors.append(f"{where}.responses.{code_str}: 既无 $ref 也无 content")


def _check_operations(doc: dict, errors: list[str]) -> int:
    """遍历 paths 下全部 operation：operationId 存在/唯一 + responses 检查。

    Returns:
        operation 总数。
    """
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        errors.append("paths: 缺失或不是映射")
        return 0

    seen: dict[str, str] = {}
    op_count = 0
    for path, item in paths.items():
        if not isinstance(item, dict):
            errors.append(f"paths.{path}: 不是映射")
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if op is None:
                continue
            op_count += 1
            where = f"paths.{path}.{method}"
            op_id = op.get("operationId") if isinstance(op, dict) else None
            if not op_id:
                errors.append(f"{where}: 缺少 operationId")
            elif op_id in seen:
                errors.append(f"{where}: operationId {op_id!r} 重复（已见于 {seen[op_id]}）")
            else:
                seen[op_id] = where
            if isinstance(op, dict):
                _check_responses(op, where, errors)
    return op_count


def main() -> int:
    """入口：解析规格并执行全部语义检查，失败列出具体路径与原因。"""
    if not SPEC_PATH.is_file():
        print(f"ERROR: {SPEC_PATH.relative_to(REPO_ROOT)} 不存在", file=sys.stderr)
        return 1
    try:
        doc = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"ERROR: api/openapi/openapi.yaml: YAML 解析失败: {exc}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict) or not str(doc.get("openapi", "")).startswith("3."):
        print("ERROR: openapi 字段缺失或不是 3.x", file=sys.stderr)
        return 1

    errors: list[str] = []
    op_count = _check_operations(doc, errors)
    _walk_refs(doc.get("components"), doc, "components", errors)
    _walk_refs(doc.get("paths"), doc, "paths", errors)

    if errors:
        print(f"FAIL: {len(errors)} 个语义错误", file=sys.stderr)
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(
        f"OK: OpenAPI {doc.get('openapi')} 语义校验通过（{op_count} 个 operation，"
        "operationId 唯一、$ref 可解析、响应族完备）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
