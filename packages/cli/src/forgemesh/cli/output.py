"""输出：JSON、YAML、推理服务表格与 ``describe`` 的详情。

表格、详情与错误消息中的字符串来自 API（资源名、条件消息等），写到终端之前替换其中的
控制字符，避免转义序列改动终端。JSON 与 YAML 输出由序列化器转义。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

import yaml

#: 表格与详情中缺失或为空的值。
NONE = "<none>"


def printable(value: object) -> str:
    """``value`` 的字符串形式，不可打印的字符（如 ESC）替换为 ``?``。"""
    return "".join(char if char.isprintable() else "?" for char in str(value))


def resource_ref(namespace: str | None, name: str) -> str:
    """消息中的资源名：``<namespace>/<name>``，不带命名空间时只有名称。"""
    return name if namespace is None else f"{namespace}/{name}"


def to_json(value: Any) -> str:
    """缩进两格的 JSON（保留非 ASCII 字符）。"""
    return json.dumps(value, indent=2, ensure_ascii=False)


def to_yaml(value: Any) -> str:
    """一个 YAML 文档，键按 API 返回的顺序排列。"""
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def to_yaml_stream(values: Sequence[Any]) -> str:
    """以 ``---`` 分隔的多个 YAML 文档，可以直接交给 ``apply -f -``。"""
    return yaml.safe_dump_all(values, sort_keys=False, allow_unicode=True, explicit_start=True)


def ready_condition(service: dict[str, Any]) -> dict[str, Any] | None:
    """``status.conditions`` 中 ``type`` 为 ``Ready`` 的条件；没有时返回 ``None``。"""
    conditions = _list(_mapping(service.get("status")).get("conditions"))
    return next((c for c in conditions if isinstance(c, dict) and c.get("type") == "Ready"), None)


def service_table(services: Iterable[dict[str, Any]]) -> str:
    """推理服务的表格：命名空间、名称、phase、``Ready`` 条件及其原因、模型。"""
    rows = [("NAMESPACE", "NAME", "PHASE", "READY", "REASON", "MODEL")]
    for service in services:
        metadata = _mapping(service.get("metadata"))
        ready = ready_condition(service) or {}
        values = (
            metadata.get("namespace"),
            metadata.get("name"),
            _mapping(service.get("status")).get("phase"),
            ready.get("status"),
            ready.get("reason"),
            _mapping(_mapping(service.get("spec")).get("model")).get("reference"),
        )
        rows.append(tuple(_cell(value) for value in values))
    return _columns(rows)


def describe_service(service: dict[str, Any]) -> str:
    """便于阅读的推理服务详情：元数据、期望状态的要点、phase、条件与实例地址。"""
    metadata = _mapping(service.get("metadata"))
    spec = _mapping(service.get("spec"))
    status = _mapping(service.get("status"))
    model = _mapping(spec.get("model"))
    runtime = _mapping(spec.get("runtime"))
    generation = f"{metadata.get('generation')} (observed {metadata.get('observedGeneration')})"
    fields = [
        ("Name", metadata.get("name")),
        ("Namespace", metadata.get("namespace")),
        ("UID", metadata.get("uid")),
        ("Tenant", metadata.get("tenantId")),
        ("Labels", _pairs(metadata.get("labels"))),
        ("Annotations", _pairs(metadata.get("annotations"))),
        ("Resource version", metadata.get("resourceVersion")),
        ("Generation", generation),
        ("Model", _joined("@", model.get("reference"), model.get("digest"))),
        ("Runtime", _joined(" ", runtime.get("provider"), runtime.get("version"))),
        ("Accelerator", _accelerator(spec)),
        ("Phase", status.get("phase")),
    ]
    width = max(len(label) for label, _ in fields) + 2
    lines = [f"{label + ':':<{width}}{_cell(value)}" for label, value in fields]
    conditions = [c for c in _list(status.get("conditions")) if isinstance(c, dict)]
    lines += _section(
        "Conditions",
        ("TYPE", "STATUS", "REASON", "LAST TRANSITION", "MESSAGE"),
        [
            (
                c.get("type"),
                c.get("status"),
                c.get("reason"),
                c.get("lastTransitionTime"),
                c.get("message"),
            )
            for c in conditions
        ],
    )
    endpoints = [e for e in _list(status.get("endpoints")) if isinstance(e, dict)]
    lines += _section(
        "Endpoints",
        ("URL", "READY", "PROTOCOL", "INSTANCE"),
        [(e.get("url"), e.get("ready"), e.get("protocol"), e.get("instanceId")) for e in endpoints],
    )
    return "\n".join(lines)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _cell(value: Any) -> str:
    if value is None or value == "":
        return NONE
    if isinstance(value, bool):
        return "true" if value else "false"
    return printable(value)


def _joined(separator: str, *parts: Any) -> str | None:
    return separator.join(str(part) for part in parts if part is not None and part != "") or None


def _pairs(value: Any) -> str | None:
    return ", ".join(f"{key}={item}" for key, item in _mapping(value).items()) or None


def _accelerator(spec: dict[str, Any]) -> str | None:
    accelerator = _mapping(_mapping(spec.get("resources")).get("accelerator"))
    if not accelerator:
        return None
    return (
        f"{accelerator.get('count')} x {accelerator.get('type')}, "
        f"at least {accelerator.get('minMemoryGiB')} GiB each"
    )


def _section(title: str, header: tuple[str, ...], rows: list[tuple[Any, ...]]) -> list[str]:
    if not rows:
        return [f"{title}:  {NONE}"]
    table = _columns([header, *(tuple(_cell(value) for value in row) for row in rows)])
    return [f"{title}:", *(f"  {line}" for line in table.splitlines())]


def _columns(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return "\n".join(
        "   ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )
