"""按环境变量选择推理实例的启动器，以及控制器核对实例的间隔。

* ``FORGEMESH_RUNTIME_LAUNCHER``：``fake``（:class:`~.fake.FakeLauncher`，控制平面进程内的
  OpenAI 兼容桩服务，供 CI 与本地开发）、``vllm-process``
  （:class:`~.vllm_process.VLLMProcessLauncher`，本机上的 vLLM 子进程）或 ``tgi-process``
  （:class:`~.tgi_process.TGIProcessLauncher`，本机上的 TGI 子进程）；不区分大小写。
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
  设备（见 :func:`~.process.cuda_device`）。
* ``tgi-process``：``FORGEMESH_TGI_COMMAND`` 为启动 TGI 的命令（拆分规则同上；默认
  ``text-generation-launcher``，须能在 ``PATH`` 中找到；否则第一个词须是可执行文件）；
  ``FORGEMESH_TGI_BIND_HOST`` / ``FORGEMESH_TGI_ADVERTISE_HOST``、
  ``FORGEMESH_TGI_READY_TIMEOUT_SECONDS``（默认 ``1200``）与
  ``FORGEMESH_TGI_STOP_GRACE_SECONDS``（默认 ``10``）的含义同 ``vllm-process``，对清单的
  要求也相同。
* ``vllm-process`` 与 ``tgi-process`` 把实例进程记在 ``record_file`` 中（应用使用数据库旁的
  ``forgemesh.instances.json``），控制平面被杀死之后，下次启动时结束其中仍在运行的进程（见
  :mod:`~.process_records`）。
* ``FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS``（默认 ``10``，见
  :func:`instance_check_interval_from_env`）：选择了启动器时，控制器每隔这么多秒经启动器
  核对一次 ``Ready`` / ``Degraded`` 服务的实例（见 :func:`~.recovery.check_instances`），
  实例退出之后的下一次核对把服务转为 ``Degraded``。

未选中的启动器的变量不读取。空白值视同未设置。
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING

from ..scheduler.inventory import INVENTORY_FILE_ENV
from . import fake, tgi_process, vllm_process
from .fake import FakeLauncher
from .launcher import RuntimeLauncher
from .network import check_advertise_host, check_bind_host, resolve_hosts
from .process import (
    DEFAULT_BIND_HOST,
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_STOP_GRACE_SECONDS,
    ProcessLauncher,
    cuda_device,
)
from .recovery import DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS
from .tgi_process import TGIProcessLauncher
from .vllm_process import VLLMProcessLauncher

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用
    from ..scheduler.inventory import Inventory

RUNTIME_LAUNCHER_ENV = "FORGEMESH_RUNTIME_LAUNCHER"
FAKE_LAUNCHER = "fake"
VLLM_PROCESS_LAUNCHER = "vllm-process"
TGI_PROCESS_LAUNCHER = "tgi-process"
FAKE_BIND_HOST_ENV = "FORGEMESH_FAKE_LAUNCHER_BIND_HOST"
FAKE_ADVERTISE_HOST_ENV = "FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST"
VLLM_COMMAND_ENV = "FORGEMESH_VLLM_COMMAND"
VLLM_BIND_HOST_ENV = "FORGEMESH_VLLM_BIND_HOST"
VLLM_ADVERTISE_HOST_ENV = "FORGEMESH_VLLM_ADVERTISE_HOST"
VLLM_READY_TIMEOUT_ENV = "FORGEMESH_VLLM_READY_TIMEOUT_SECONDS"
VLLM_STOP_GRACE_ENV = "FORGEMESH_VLLM_STOP_GRACE_SECONDS"
TGI_COMMAND_ENV = "FORGEMESH_TGI_COMMAND"
TGI_BIND_HOST_ENV = "FORGEMESH_TGI_BIND_HOST"
TGI_ADVERTISE_HOST_ENV = "FORGEMESH_TGI_ADVERTISE_HOST"
TGI_READY_TIMEOUT_ENV = "FORGEMESH_TGI_READY_TIMEOUT_SECONDS"
TGI_STOP_GRACE_ENV = "FORGEMESH_TGI_STOP_GRACE_SECONDS"
INSTANCE_CHECK_INTERVAL_ENV = "FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS"


class LauncherConfigError(ValueError):
    """启动器配置非法；服务应拒绝启动。"""


@dataclass(frozen=True)
class _ProcessVariables:
    bind_host: str
    advertise_host: str
    ready_timeout: str
    stop_grace: str


_VLLM_VARIABLES = _ProcessVariables(
    VLLM_BIND_HOST_ENV, VLLM_ADVERTISE_HOST_ENV, VLLM_READY_TIMEOUT_ENV, VLLM_STOP_GRACE_ENV
)
_TGI_VARIABLES = _ProcessVariables(
    TGI_BIND_HOST_ENV, TGI_ADVERTISE_HOST_ENV, TGI_READY_TIMEOUT_ENV, TGI_STOP_GRACE_ENV
)


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


def _configured_command(env: Mapping[str, str], name: str) -> tuple[str, ...] | None:
    raw = _get(env, name)
    if raw is None:
        return None
    try:
        command = tuple(shlex.split(raw))
    except ValueError as exc:
        raise LauncherConfigError(f"invalid {name}: {exc}") from None
    if shutil.which(command[0]) is None:
        raise LauncherConfigError(f"invalid {name}: {command[0]!r} is not an executable file")
    return command


def _vllm_command(env: Mapping[str, str]) -> tuple[str, ...]:
    command = _configured_command(env, VLLM_COMMAND_ENV)
    if command is not None:
        return command
    if find_spec("vllm") is None:
        raise LauncherConfigError(
            f"{RUNTIME_LAUNCHER_ENV}={VLLM_PROCESS_LAUNCHER} runs "
            f"{' '.join(vllm_process.DEFAULT_COMMAND)} by default, but the vllm package is not "
            f"installed for this interpreter; install vllm or point {VLLM_COMMAND_ENV} to a vLLM "
            "installation"
        )
    return vllm_process.DEFAULT_COMMAND


def _tgi_command(env: Mapping[str, str]) -> tuple[str, ...]:
    command = _configured_command(env, TGI_COMMAND_ENV)
    if command is not None:
        return command
    [executable] = tgi_process.DEFAULT_COMMAND
    if shutil.which(executable) is None:
        raise LauncherConfigError(
            f"{RUNTIME_LAUNCHER_ENV}={TGI_PROCESS_LAUNCHER} runs {executable} by default, but "
            f"it is not on PATH; install TGI or point {TGI_COMMAND_ENV} to its {executable}"
        )
    return tgi_process.DEFAULT_COMMAND


def _process_node(inventory: Inventory, launcher: type[ProcessLauncher]) -> str:
    if len(inventory.nodes) != 1:
        raise LauncherConfigError(
            f"{RUNTIME_LAUNCHER_ENV}={launcher.launcher_name} runs instances on the control "
            f"plane host only, so {INVENTORY_FILE_ENV} must describe exactly one node (this "
            f"host), not {len(inventory.nodes)}: {', '.join(node.id for node in inventory.nodes)}"
        )
    [node] = inventory.nodes
    invalid = f"invalid {INVENTORY_FILE_ENV} for {launcher.runtime_name}"
    selected: dict[str, str] = {}
    for device in node.devices:
        try:
            visible = cuda_device(device.id)
        except ValueError as exc:
            raise LauncherConfigError(f"{invalid}: {exc}") from None
        if visible in selected:
            raise LauncherConfigError(
                f"{invalid}: device ids {selected[visible]!r} and {device.id!r} both select "
                f"CUDA device {visible}"
            )
        selected[visible] = device.id
    return node.id


def _process_launcher(
    launcher: type[ProcessLauncher],
    inventory: Inventory,
    env: Mapping[str, str],
    command_from_env: Callable[[Mapping[str, str]], tuple[str, ...]],
    variables: _ProcessVariables,
    record_file: str | None,
) -> ProcessLauncher:
    node_id = _process_node(inventory, launcher)
    command = command_from_env(env)
    bind, advertise = _hosts(env, variables.bind_host, variables.advertise_host, DEFAULT_BIND_HOST)
    return launcher(
        node_id,
        command=command,
        bind_host=bind,
        advertise_host=advertise,
        ready_timeout=_seconds(env, variables.ready_timeout, DEFAULT_READY_TIMEOUT_SECONDS),
        stop_grace=_seconds(env, variables.stop_grace, DEFAULT_STOP_GRACE_SECONDS),
        record_file=record_file,
    )


def launcher_from_env(
    inventory: Inventory | None,
    environ: Mapping[str, str] | None = None,
    *,
    record_file: str | None = None,
) -> RuntimeLauncher | None:
    """按环境变量构造启动器；未设置 ``FORGEMESH_RUNTIME_LAUNCHER`` 时返回 ``None``。

    Args:
        inventory: 已加载的加速器清单（:func:`~forgemesh.controlplane.scheduler.inventory_from_env`），
            未配置时为 ``None``。
        environ: 环境变量，默认 :data:`os.environ`。
        record_file: ``vllm-process`` / ``tgi-process`` 记录实例进程的文件（见
            :mod:`~.process_records`，应用使用数据库旁的
            :func:`~.process_records.record_file_path`）；为空时不记录。

    Raises:
        LauncherConfigError: 启动器未知、缺少清单，或所选启动器的配置非法（见模块说明）。
    """
    env = os.environ if environ is None else environ
    name = (_get(env, RUNTIME_LAUNCHER_ENV) or "").lower()
    if not name:
        return None
    if name not in (FAKE_LAUNCHER, VLLM_PROCESS_LAUNCHER, TGI_PROCESS_LAUNCHER):
        raise LauncherConfigError(
            f"unsupported {RUNTIME_LAUNCHER_ENV}={name!r}; expected {FAKE_LAUNCHER!r}, "
            f"{VLLM_PROCESS_LAUNCHER!r} or {TGI_PROCESS_LAUNCHER!r}"
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
    if name == VLLM_PROCESS_LAUNCHER:
        return _process_launcher(
            VLLMProcessLauncher, inventory, env, _vllm_command, _VLLM_VARIABLES, record_file
        )
    return _process_launcher(
        TGIProcessLauncher, inventory, env, _tgi_command, _TGI_VARIABLES, record_file
    )


def instance_check_interval_from_env(environ: Mapping[str, str] | None = None) -> float:
    """控制器两次核对已部署实例之间的秒数（``FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS``）。

    Args:
        environ: 环境变量，默认 :data:`os.environ`。

    Raises:
        LauncherConfigError: 取值不是有限的正数秒。
    """
    env = os.environ if environ is None else environ
    return _seconds(env, INSTANCE_CHECK_INTERVAL_ENV, DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS)


__all__ = [
    "FAKE_ADVERTISE_HOST_ENV",
    "FAKE_BIND_HOST_ENV",
    "FAKE_LAUNCHER",
    "INSTANCE_CHECK_INTERVAL_ENV",
    "RUNTIME_LAUNCHER_ENV",
    "TGI_ADVERTISE_HOST_ENV",
    "TGI_BIND_HOST_ENV",
    "TGI_COMMAND_ENV",
    "TGI_PROCESS_LAUNCHER",
    "TGI_READY_TIMEOUT_ENV",
    "TGI_STOP_GRACE_ENV",
    "VLLM_ADVERTISE_HOST_ENV",
    "VLLM_BIND_HOST_ENV",
    "VLLM_COMMAND_ENV",
    "VLLM_PROCESS_LAUNCHER",
    "VLLM_READY_TIMEOUT_ENV",
    "VLLM_STOP_GRACE_ENV",
    "LauncherConfigError",
    "instance_check_interval_from_env",
    "launcher_from_env",
]
