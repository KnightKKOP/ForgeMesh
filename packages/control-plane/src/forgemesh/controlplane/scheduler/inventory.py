"""静态加速器清单：调度器可分配的节点与设备，来自 ``FORGEMESH_INVENTORY_FILE``。

控制平面启动时读取一次清单文件（JSON）并严格校验：未知字段、类型不符（不做隐式
转换，如 ``"80"`` / ``80.0`` 不是合法的 ``memoryGiB``）、空列表、重复的节点 id、
同一节点内重复的设备 id 都会让启动失败。修改清单后需重启控制平面。

文件格式::

    {"nodes": [{"id": "node-a",
                "devices": [{"id": "gpu-0", "type": "nvidia-gpu", "memoryGiB": 80},
                            {"id": "gpu-1", "type": "nvidia-gpu", "memoryGiB": 80}]}]}

* ``type`` 与 ``spec.resources.accelerator.type`` 按字符串精确匹配；
* ``memoryGiB`` 为单卡显存（GiB），与 ``spec.resources.accelerator.minMemoryGiB`` 比较；
* 设备以 ``(节点 id, 设备 id)`` 标识；节点与设备的先后顺序即首次适配的挑选顺序。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

INVENTORY_FILE_ENV = "FORGEMESH_INVENTORY_FILE"


class InventoryConfigError(ValueError):
    """加速器清单文件不可读或内容非法；服务应拒绝启动。"""


# 非空检查不用 ``Field(min_length=1)``：元组中某一项校验失败时，pydantic 还会对外层元组
# 报一条误导性的 too_short 错误。
def _check_ids(what: str, ids: list[str]) -> None:
    if not ids:
        raise ValueError(f"at least one {what} is required")
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {what} ids: {duplicates}")


class InventoryDevice(BaseModel):
    """清单中的一个加速器设备。

    Attributes:
        id: 设备标识，在所属节点内唯一。
        type: 加速器类型，如 ``nvidia-gpu``。
        memoryGiB: 单卡显存（GiB）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    memoryGiB: int = Field(ge=1)


class InventoryNode(BaseModel):
    """清单中的一个节点及其设备。

    Attributes:
        id: 节点标识，在清单内唯一。
        devices: 节点上的设备（至少一个）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(min_length=1)
    devices: tuple[InventoryDevice, ...]

    @field_validator("devices")
    @classmethod
    def _check_devices(cls, value: tuple[InventoryDevice, ...]) -> tuple[InventoryDevice, ...]:
        _check_ids("device", [device.id for device in value])
        return value


class Inventory(BaseModel):
    """静态加速器清单（清单文件的顶层结构）。

    Attributes:
        nodes: 节点列表（至少一个）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    nodes: tuple[InventoryNode, ...]

    @field_validator("nodes")
    @classmethod
    def _check_nodes(cls, value: tuple[InventoryNode, ...]) -> tuple[InventoryNode, ...]:
        _check_ids("node", [node.id for node in value])
        return value


def load_inventory(path: str | os.PathLike[str]) -> Inventory:
    """读取并校验清单文件。

    Raises:
        InventoryConfigError: 文件不可读，或内容不符合清单格式。
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise InventoryConfigError(f"cannot read inventory file {path}: {exc}") from exc
    try:
        return Inventory.model_validate_json(raw)
    except ValidationError as exc:
        raise InventoryConfigError(f"invalid inventory file {path}: {exc}") from exc


def inventory_from_env(environ: Mapping[str, str] | None = None) -> Inventory | None:
    """按 ``FORGEMESH_INVENTORY_FILE`` 加载清单；未设置或为空时返回 ``None``（不启用调度器）。

    Raises:
        InventoryConfigError: 已设置，但文件不可读或内容非法。
    """
    env = os.environ if environ is None else environ
    path = (env.get(INVENTORY_FILE_ENV) or "").strip()
    if not path:
        return None
    return load_inventory(path)


__all__ = [
    "INVENTORY_FILE_ENV",
    "Inventory",
    "InventoryConfigError",
    "InventoryDevice",
    "InventoryNode",
    "inventory_from_env",
    "load_inventory",
]
