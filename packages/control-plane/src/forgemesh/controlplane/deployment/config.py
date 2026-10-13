"""按环境变量选择推理实例的启动器。

* ``FORGEMESH_RUNTIME_LAUNCHER``：``fake``（:class:`~.fake.FakeLauncher`，控制平面进程内的
  OpenAI 兼容桩服务，供 CI 与本地开发）或 ``vllm-process``
  （:class:`~.vllm_process.VLLMProcessLauncher`，本机上的 vLLM 子进程）；不区分大小写。
  未设置或为空时不选择启动器（应用不部署实例）；其他取值拒绝启动。选择了启动器就必须
  同时设置 ``FORGEMESH_INVENTORY_FILE``，否则没有服务能被调度。
* ``fake``：``FORGEMESH_FAKE_LAUNCHER_BIND_HOST`` 为实例监听的 IP 地址（默认
  ``127.0.0.1``；``0.0.0.0`` / ``::`` 监听全部地址）；``FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST``
  为写进实例地址的主机名或 IP 地址（默认同监听地址，监听全部地址时必须设置）。
* ``vllm-process``：``FORGEMESH_VLLM_COMMAND`` 为启动 vLLM 的命令（按 shell 规则拆分，
  不经过 shell；默认 ``<控制平面的解释器> -m vllm.entrypoints.openai.api_server``，此时
  该解释器必须装有 ``vllm``；否则第一个词须是可执行文件）；``FORGEMESH_VLLM_BIND_HOST`` /
  ``FORGEMESH_VLLM_ADVERTISE_HOST`` 同上；``FORGEMESH_VLLM_READY_TIMEOUT_SECONDS``（默认
  ``1200``）为进程启动后等它就绪的时限；``FORGEMESH_VLLM_STOP_GRACE_SECONDS``（默认
  ``10``）为 ``SIGTERM`` 之后的宽限期。实例只运行在控制平面所在的主机上，因此清单必须
  恰好有一个节点，其设备 id 都能写进 ``CUDA_VISIBLE_DEVICES``，且不会有两个 id 选中同一块
  设备（见 :func:`~.vllm_process.cuda_device`）。

未选中的启动器的变量不读取。空白值视同未设置。
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
from collections.abc import Mapping
from importlib.util import find_spec
from typing import TYPE_CHECKING

from ..scheduler.inventory import INVENTORY_FILE_ENV
from . import fake, vllm_process
from .fake import FakeLauncher
from .launcher import RuntimeLauncher
from .network import check_advertise_host, check_bind_host, resolve_hosts
from .vllm_process import (
    DEFAULT_COMMAND,
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_STOP_GRACE_SECONDS,
    VLLMProcessLauncher,
    cuda_device,
)

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用
    from ..scheduler.inventory import Inventory

RUNTIME_LAUNCHER_ENV = "FORGEMESH_RUNTIME_LAUNCHER"
FAKE_LAUNCHER = "fake"
VLLM_PROCESS_LAUNCHER = "vllm-process"
FAKE_BIND_HOST_ENV = "FORGEMESH_FAKE_LAUNCHER_BIND_HOST"
FAKE_ADVERTISE_HOST_ENV = "FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST"
VLLM_COMMAND_ENV = "FORGEMESH_VLLM_COMMAND"
VLLM_BIND_HOST_ENV = "FORGEMESH_VLLM_BIND_HOST"
VLLM_ADVERTISE_HOST_ENV = "FORGEMESH_VLLM_ADVERTISE_HOST"
VLLM_READY_TIMEOUT_ENV = "FORGEMESH_VLLM_READY_TIMEOUT_SECONDS"
VLLM_STOP_GRACE_ENV = "FORGEMESH_VLLM_STOP_GRACE_SECONDS"


class LauncherConfigError(ValueError):
    """启动器配置非法；服务应拒绝启动。"""


def _get(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def _hosts(
    env: Mapping[str, str], bind_env: str, advertise_env: str, default_bind: str
) -> tuple[str, str | None]:
    bind = _get(env, bind_env) or default_bind
    advertise = _get(env, advertise_env)
    try:
        check_bind_host(bind)
    except ValueError as exc:
        raise LauncherConfigError(f"invalid {bind_env}: {exc}") from None
    if advertise is not None:
        try:
            check_advertise_host(advertise)
        except ValueError as exc:
            raise LauncherConfigError(f"invalid {advertise_env}: {exc}") from None
    try:
        resolve_hosts(bind, advertise)
    except ValueError:
        raise LauncherConfigError(
            f"{advertise_env} must be set when {bind_env}={bind} listens on all addresses: "
            "instance urls need a host that the gateway can connect to"
        ) from None
    return bind, advertise


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise LauncherConfigError(f"{name}={raw!r} is not a number of seconds") from None
    if not math.isfinite(value) or value <= 0:
        raise LauncherConfigError(f"{name}={raw!r} must be a positive number of seconds")
    return value


def _vllm_command(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _get(env, VLLM_COMMAND_ENV)
    if raw is None:
        if find_spec("vllm") is None:
            raise LauncherConfigError(
                f"{RUNTIME_LAUNCHER_ENV}={VLLM_PROCESS_LAUNCHER} runs {' '.join(DEFAULT_COMMAND)} "
                f"by default, but the vllm package is not installed for this interpreter; "
                f"install vllm or point {VLLM_COMMAND_ENV} to a vLLM installation"
            )
        return DEFAULT_COMMAND
    try:
        command = tuple(shlex.split(raw))
    except ValueError as exc:
        raise LauncherConfigError(f"invalid {VLLM_COMMAND_ENV}: {exc}") from None
    if shutil.which(command[0]) is None:
        raise LauncherConfigError(
            f"invalid {VLLM_COMMAND_ENV}: {command[0]!r} is not an executable file"
        )
    return command


def _vllm_node(inventory: Inventory) -> str:
    if len(inventory.nodes) != 1:
        raise LauncherConfigError(
            f"{RUNTIME_LAUNCHER_ENV}={VLLM_PROCESS_LAUNCHER} runs instances on the control plane "
            f"host only, so {INVENTORY_FILE_ENV} must describe exactly one node (this host), "
            f"not {len(inventory.nodes)}: {', '.join(node.id for node in inventory.nodes)}"
        )
    [node] = inventory.nodes
    selected: dict[str, str] = {}
    for device in node.devices:
        try:
            visible = cuda_device(device.id)
        except ValueError as exc:
            raise LauncherConfigError(f"invalid {INVENTORY_FILE_ENV} for vLLM: {exc}") from None
        if visible in selected:
            raise LauncherConfigError(
                f"invalid {INVENTORY_FILE_ENV} for vLLM: device ids {selected[visible]!r} and "
                f"{device.id!r} both select CUDA device {visible}"
            )
        selected[visible] = device.id
    return node.id


def launcher_from_env(
    inventory: Inventory | None, environ: Mapping[str, str] | None = None
) -> RuntimeLauncher | None:
    """按环境变量构造启动器；未设置 ``FORGEMESH_RUNTIME_LAUNCHER`` 时返回 ``None``。

    Args:
        inventory: 已加载的加速器清单（:func:`~forgemesh.controlplane.scheduler.inventory_from_env`），
            未配置时为 ``None``。
        environ: 环境变量，默认 :data:`os.environ`。

    Raises:
        LauncherConfigError: 启动器未知、缺少清单，或所选启动器的配置非法（见模块说明）。
    """
    env = os.environ if environ is None else environ
    name = (_get(env, RUNTIME_LAUNCHER_ENV) or "").lower()
    if not name:
        return None
    if name not in (FAKE_LAUNCHER, VLLM_PROCESS_LAUNCHER):
        raise LauncherConfigError(
            f"unsupported {RUNTIME_LAUNCHER_ENV}={name!r}; expected {FAKE_LAUNCHER!r} or "
            f"{VLLM_PROCESS_LAUNCHER!r}"
        )
    if inventory is None:
        raise LauncherConfigError(
            f"{RUNTIME_LAUNCHER_ENV}={name} requires {INVENTORY_FILE_ENV}: without an "
            "accelerator inventory no inference service can be scheduled"
        )
    if name == FAKE_LAUNCHER:
        bind, advertise = _hosts(
            env, FAKE_BIND_HOST_ENV, FAKE_ADVERTISE_HOST_ENV, fake.DEFAULT_BIND_HOST
        )
        return FakeLauncher(bind_host=bind, advertise_host=advertise)
    node_id = _vllm_node(inventory)
    command = _vllm_command(env)
    bind, advertise = _hosts(
        env, VLLM_BIND_HOST_ENV, VLLM_ADVERTISE_HOST_ENV, vllm_process.DEFAULT_BIND_HOST
    )
    return VLLMProcessLauncher(
        node_id,
        command=command,
        bind_host=bind,
        advertise_host=advertise,
        ready_timeout=_seconds(env, VLLM_READY_TIMEOUT_ENV, DEFAULT_READY_TIMEOUT_SECONDS),
        stop_grace=_seconds(env, VLLM_STOP_GRACE_ENV, DEFAULT_STOP_GRACE_SECONDS),
    )


__all__ = [
    "FAKE_ADVERTISE_HOST_ENV",
    "FAKE_BIND_HOST_ENV",
    "FAKE_LAUNCHER",
    "RUNTIME_LAUNCHER_ENV",
    "VLLM_ADVERTISE_HOST_ENV",
    "VLLM_BIND_HOST_ENV",
    "VLLM_COMMAND_ENV",
    "VLLM_PROCESS_LAUNCHER",
    "VLLM_READY_TIMEOUT_ENV",
    "VLLM_STOP_GRACE_ENV",
    "LauncherConfigError",
    "launcher_from_env",
]
