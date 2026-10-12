"""静态加速器清单单元测试：``FORGEMESH_INVENTORY_FILE`` 的加载与严格校验。

清单不可读或内容不合法（未知字段、类型不符且不做隐式转换、空列表、重复 id、JSON
损坏）时抛 :class:`InventoryConfigError`，控制平面据此拒绝启动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from forgemesh.controlplane.scheduler import (
    INVENTORY_FILE_ENV,
    InventoryConfigError,
    inventory_from_env,
    load_inventory,
)


def _device(device_id: str = "gpu-0", **overrides: Any) -> dict[str, Any]:
    return {"id": device_id, "type": "nvidia-gpu", "memoryGiB": 80, **overrides}


def _node(*devices: dict[str, Any], node_id: str = "node-a", **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "devices": list(devices), **extra}


def _write(tmp_path: Path, content: Any) -> Path:
    path = tmp_path / "inventory.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    return path


@pytest.mark.unit
def test_inventory_keeps_nodes_and_devices_in_file_order(tmp_path):
    """节点与设备保持文件中的顺序（即首次适配顺序）；设备 id 只需在节点内唯一。"""
    path = _write(
        tmp_path,
        {
            "nodes": [
                _node(_device("gpu-1", memoryGiB=24), _device("gpu-0"), node_id="node-b"),
                _node(_device("gpu-0", type="amd-gpu", memoryGiB=64), node_id="node-a"),
            ]
        },
    )

    inventory = load_inventory(path)

    assert [node.id for node in inventory.nodes] == ["node-b", "node-a"]
    assert [
        [(device.id, device.type, device.memoryGiB) for device in node.devices]
        for node in inventory.nodes
    ] == [
        [("gpu-1", "nvidia-gpu", 24), ("gpu-0", "nvidia-gpu", 80)],
        [("gpu-0", "amd-gpu", 64)],
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(
            {"nodes": [_node(_device())], "version": 1}, "type=extra_forbidden", id="top-level"
        ),
        pytest.param({"nodes": [_node(_device(), zone="z")]}, "type=extra_forbidden", id="node"),
        pytest.param(
            {"nodes": [_node(_device(vendor="nvidia"))]}, "type=extra_forbidden", id="device"
        ),
        pytest.param(
            {"nodes": [_node({"id": "gpu-0", "type": "nvidia-gpu", "memory_gib": 80})]},
            "type=extra_forbidden",
            id="snake-case-memory",
        ),
        pytest.param(
            {"nodes": [_node(_device(memory_gib=40))]},
            "type=extra_forbidden",
            id="snake-case-alongside",
        ),
        pytest.param({"nodes": [_node(_device(memoryGiB="80"))]}, "type=int_type", id="mem-str"),
        pytest.param({"nodes": [_node(_device(memoryGiB=80.0))]}, "type=int_type", id="mem-float"),
        pytest.param({"nodes": [_node(_device(memoryGiB=True))]}, "type=int_type", id="mem-bool"),
        pytest.param(
            {"nodes": [_node(_device(memoryGiB=0))]}, "type=greater_than_equal", id="mem-zero"
        ),
        pytest.param(
            {"nodes": [_node({"id": "gpu-0", "memoryGiB": 80})]}, "type=missing", id="no-type"
        ),
        pytest.param({"nodes": [_node(_device(type=""))]}, "type=string_too_short", id="type"),
        pytest.param({"nodes": [_node(_device(""))]}, "type=string_too_short", id="device-id"),
        pytest.param(
            {"nodes": [_node(_device(), node_id="")]}, "type=string_too_short", id="node-id"
        ),
        pytest.param({}, "type=missing", id="no-nodes-field"),
        pytest.param({"nodes": []}, "at least one node is required", id="no-nodes"),
        pytest.param({"nodes": [_node()]}, "at least one device is required", id="no-devices"),
        pytest.param({"nodes": _node(_device())}, "type=tuple_type", id="nodes-object"),
        pytest.param(
            {"nodes": [_node(_device()), _node(_device("gpu-1"))]},
            "duplicate node ids: ['node-a']",
            id="duplicate-nodes",
        ),
        pytest.param(
            {"nodes": [_node(_device(), _device())]},
            "duplicate device ids: ['gpu-0']",
            id="duplicate-devices",
        ),
        pytest.param([_node(_device())], "type=model_type", id="array"),
        pytest.param(b"", "type=json_invalid", id="empty-file"),
        pytest.param(b'{"nodes": [', "type=json_invalid", id="truncated"),
        pytest.param(b"\xff\xfe", "type=json_invalid", id="not-utf8"),
    ],
)
def test_invalid_inventory_is_rejected(tmp_path, content, expected):
    path = _write(tmp_path, content)

    with pytest.raises(InventoryConfigError) as excinfo:
        load_inventory(path)

    message = str(excinfo.value)
    assert message.startswith(f"invalid inventory file {path}:")
    assert expected in message


@pytest.mark.unit
@pytest.mark.parametrize("target", ["missing.json", "."], ids=["missing", "directory"])
def test_unreadable_inventory_file_is_rejected(tmp_path, target):
    path = tmp_path / target

    with pytest.raises(InventoryConfigError, match="cannot read inventory file"):
        load_inventory(path)


@pytest.mark.unit
@pytest.mark.parametrize("environ", [{}, {INVENTORY_FILE_ENV: ""}, {INVENTORY_FILE_ENV: "  "}])
def test_inventory_from_env_is_disabled_when_unset_or_blank(environ):
    assert inventory_from_env(environ) is None


@pytest.mark.unit
def test_inventory_from_env_loads_the_configured_file(tmp_path, monkeypatch):
    path = _write(tmp_path, {"nodes": [_node(_device())]})

    configured = inventory_from_env({INVENTORY_FILE_ENV: f" {path} "})
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(path))

    assert configured is not None
    assert [node.id for node in configured.nodes] == ["node-a"]
    assert inventory_from_env() == configured


@pytest.mark.unit
@pytest.mark.parametrize("content", [None, {"nodes": []}], ids=["missing-file", "invalid-file"])
def test_inventory_from_env_rejects_a_configured_but_unusable_file(tmp_path, content):
    path = tmp_path / "inventory.json" if content is None else _write(tmp_path, content)

    with pytest.raises(InventoryConfigError):
        inventory_from_env({INVENTORY_FILE_ENV: str(path)})
