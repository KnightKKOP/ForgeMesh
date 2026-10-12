"""vLLM 进程启动器：每个实例是控制平面所在主机上的一个 vLLM OpenAI 兼容服务进程。

:class:`VLLMProcessLauncher` 以 ``<command> --model=<spec.model.reference> --host <bind_host>
--port <port> --tensor-parallel-size <设备数>`` 启动实例。``command`` 默认是
``<控制平面的解释器> -m vllm.entrypoints.openai.api_server``，可换成其他环境中的 vLLM（或
测试用的替身）；端口由操作系统挑选。实例继承控制平面的环境变量（去掉 ``FORGEMESH_*``），
``CUDA_VISIBLE_DEVICES`` 取自分配的设备，未设置 ``CUDA_DEVICE_ORDER`` 时设为
``PCI_BUS_ID``，设备序号因此与 ``nvidia-smi`` 一致。实例的标准输出与标准错误即控制平面
的输出。只服务 ``spec.runtime.provider`` 为 ``vllm`` 的资源；``spec.runtime.version`` 与
``spec.model.digest`` 都不检查：运行的是 ``command`` 对应的 vLLM，模型由 vLLM 按引用加载。

实例在哪里运行
--------------
实例是控制平面的子进程，只能运行在控制平面所在的主机上：加速器清单必须恰好有一个节点
（就是这台主机），分配的设备都属于该节点（``node_id``）。清单中的设备 id 必须能写进
``CUDA_VISIBLE_DEVICES``：CUDA 设备序号（``0``）、带 ``gpu-`` 前缀的序号（``gpu-0``），
或以 ``GPU-`` / ``MIG-`` 开头的 UUID（原样使用），见 :func:`cuda_device`。

就绪与停止
----------
``verify`` 每隔 ``poll_interval`` 秒请求一次 ``GET /health``，返回 ``200`` 即就绪；进程
退出、或进程启动 ``ready_timeout`` 秒后仍未就绪时抛出。每个实例在自己的会话（进程组）中
运行：``stop`` 向整个进程组发送 ``SIGTERM``，``stop_grace`` 秒内没有退出就发送
``SIGKILL``，vLLM 派生的工作进程一并结束。

控制平面重启
------------
实例随控制平面结束：``aclose`` 停止全部实例，容器中整个进程树随容器结束。重启之后启动器
不认识重启之前的实例。直接在主机上运行、控制平面被 ``SIGKILL`` 时，实例进程（不在控制
平面的进程组中）不会随之结束、继续占用 GPU：请以容器或 systemd 服务（默认
``KillMode=control-group``）运行控制平面。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import signal
import socket
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from forgemesh.resource.inference import EndpointProtocol

from .launcher import LaunchedInstance, LaunchRequest, RuntimeLauncher
from .network import instance_url, local_address, probe_health, resolve_hosts, socket_family

logger = logging.getLogger(__name__)

#: vLLM 的 OpenAI 兼容服务入口模块。
VLLM_MODULE = "vllm.entrypoints.openai.api_server"
#: 默认命令：以控制平面的解释器运行 vLLM。
DEFAULT_COMMAND: tuple[str, ...] = (sys.executable, "-m", VLLM_MODULE)
#: 默认监听地址（同时也是默认写进实例地址的主机）。
DEFAULT_BIND_HOST = "127.0.0.1"
#: 进程启动后等它就绪的默认时限（秒），包括下载与加载模型。
DEFAULT_READY_TIMEOUT_SECONDS = 1200.0
#: ``SIGTERM`` 之后等进程退出的默认宽限期（秒）。
DEFAULT_STOP_GRACE_SECONDS = 10.0
#: ``SIGKILL`` 之后等进程退出的时限（秒）；超过即 :meth:`~VLLMProcessLauncher.stop` 失败。
KILL_WAIT_SECONDS = 10.0
#: 服务的运行时（``spec.runtime.provider``）。
PROVIDER = "vllm"

_INDEX_DEVICE = re.compile(r"(?:gpu-)?([0-9]+)")
_UUID_DEVICE = re.compile(r"(?:GPU|MIG)-[0-9A-Za-z][0-9A-Za-z/-]*")
_PORT_ATTEMPTS = 20


def cuda_device(device_id: str) -> str:
    """清单中的设备 id 在 ``CUDA_VISIBLE_DEVICES`` 中的写法。

    ``0`` / ``gpu-0`` → ``0``（CUDA 设备序号）；``GPU-…`` / ``MIG-…``（UUID）原样返回。

    Raises:
        ValueError: 不是上述格式。
    """
    if match := _INDEX_DEVICE.fullmatch(device_id):
        return str(int(match.group(1)))
    if _UUID_DEVICE.fullmatch(device_id):
        return device_id
    raise ValueError(
        f"device id {device_id!r} is neither a CUDA device index (0, gpu-0) nor a GPU-/MIG- UUID"
    )


def _positive(name: str, value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds, got {value!r}")
    return value


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


@dataclass(eq=False)
class _Process:
    key: str
    instance: LaunchedInstance
    process: asyncio.subprocess.Process
    port: int
    ready_deadline: float


class VLLMProcessLauncher(RuntimeLauncher):
    """把每个实例作为本机上的 vLLM 子进程运行的启动器（见模块说明）。

    Args:
        node_id: 控制平面所在主机在加速器清单中的节点 id；分配到其他节点的设备无法使用。
        command: 启动 vLLM OpenAI 兼容服务的命令（参数列表），默认 :data:`DEFAULT_COMMAND`。
        bind_host: 实例监听的 IP 地址，默认 ``127.0.0.1``；``0.0.0.0`` / ``::`` 监听全部地址。
        advertise_host: 写进实例地址的主机名或 IP 地址，默认同 ``bind_host``；监听全部
            地址时必须给出。
        ready_timeout: 进程启动后等它就绪的时限（秒）。
        stop_grace: ``SIGTERM`` 之后等进程退出的宽限期（秒），之后 ``SIGKILL``。
        poll_interval: 等待就绪时两次 ``GET /health`` 之间的秒数。

    Raises:
        ValueError: 命令为空、地址非法或时间不是正数。
    """

    def __init__(
        self,
        node_id: str,
        *,
        command: Sequence[str] = DEFAULT_COMMAND,
        bind_host: str = DEFAULT_BIND_HOST,
        advertise_host: str | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        stop_grace: float = DEFAULT_STOP_GRACE_SECONDS,
        poll_interval: float = 1.0,
    ) -> None:
        if not node_id:
            raise ValueError("node_id must not be empty")
        if isinstance(command, str):
            raise ValueError("the vLLM command must be a sequence of arguments, not a string")
        if not command or not command[0]:
            raise ValueError("the vLLM command must not be empty")
        self._node_id = node_id
        self._command = tuple(command)
        self._bind_host, self._advertise_host = resolve_hosts(bind_host, advertise_host)
        self._probe_host = local_address(self._bind_host)
        self._ready_timeout = _positive("ready_timeout", ready_timeout)
        self._stop_grace = _positive("stop_grace", stop_grace)
        self._poll_interval = _positive("poll_interval", poll_interval)
        self._by_key: dict[str, _Process] = {}
        self._by_id: dict[str, _Process] = {}
        self._closed = False

    @property
    def node_id(self) -> str:
        """实例运行所在的清单节点。"""
        return self._node_id

    @property
    def command(self) -> tuple[str, ...]:
        """启动 vLLM 的命令。"""
        return self._command

    @property
    def bind_host(self) -> str:
        """实例监听的地址。"""
        return self._bind_host

    @property
    def advertise_host(self) -> str:
        """写进实例地址的主机。"""
        return self._advertise_host

    @property
    def ready_timeout(self) -> float:
        """进程启动后等它就绪的时限（秒）。"""
        return self._ready_timeout

    @property
    def stop_grace(self) -> float:
        """``SIGTERM`` 之后的宽限期（秒）。"""
        return self._stop_grace

    async def launch(self, request: LaunchRequest) -> LaunchedInstance:
        """启动 vLLM 进程并立即返回；``instance_key`` 的进程仍在运行时返回它。

        Raises:
            ValueError: 资源不是 vLLM 运行时、模型引用为空、设备不在本节点或设备 id 无法
                写进 ``CUDA_VISIBLE_DEVICES``。
            RuntimeError: 启动器已关闭，或找不到空闲端口。
            OSError: 无法启动命令。
        """
        if self._closed:
            raise RuntimeError("the vLLM process launcher is closed")
        existing = self._by_key.get(request.instance_key)
        if existing is not None:
            if existing.process.returncode is None:
                return existing.instance
            await self._terminate(existing)
            self._forget(existing)
        devices = self._cuda_devices(request)
        port = self._free_port()
        instance = LaunchedInstance(
            instance_id=f"vllm-{uuid.uuid4().hex[:12]}",
            url=instance_url(self._advertise_host, port),
            protocol=EndpointProtocol.OPENAI_CHAT,
        )
        env = {
            name: value for name, value in os.environ.items() if not name.startswith("FORGEMESH_")
        }
        env["CUDA_VISIBLE_DEVICES"] = ",".join(devices)
        env.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
        process = await asyncio.create_subprocess_exec(
            *self._command,
            f"--model={request.spec.model.reference}",
            "--host",
            self._bind_host,
            "--port",
            str(port),
            "--tensor-parallel-size",
            str(max(len(devices), 1)),
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        state = _Process(
            key=request.instance_key,
            instance=instance,
            process=process,
            port=port,
            ready_deadline=asyncio.get_running_loop().time() + self._ready_timeout,
        )
        self._by_key[state.key] = state
        self._by_id[instance.instance_id] = state
        logger.info(
            "vLLM instance started: instance_id=%s pid=%d url=%s model=%s "
            "CUDA_VISIBLE_DEVICES=%s instance_key=%s",
            instance.instance_id,
            process.pid,
            instance.url,
            request.spec.model.reference,
            env["CUDA_VISIBLE_DEVICES"],
            state.key,
        )
        return instance

    async def verify(self, instance: LaunchedInstance) -> None:
        """等实例的 ``GET /health`` 返回 ``200``。

        Raises:
            RuntimeError: 启动器不认识这个实例（如重启之前的实例）、进程已退出，或进程启动
                ``ready_timeout`` 秒后仍未就绪。
        """
        state = self._by_id.get(instance.instance_id)
        if state is None:
            raise RuntimeError(f"instance {instance.instance_id} is not running")
        loop = asyncio.get_running_loop()
        while True:
            returncode = state.process.returncode
            if returncode is not None:
                raise RuntimeError(
                    f"instance {instance.instance_id} exited with code {returncode} before it "
                    "became healthy"
                )
            if await probe_health(self._probe_host, state.port):
                return
            remaining = state.ready_deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    f"instance {instance.instance_id} did not become healthy within "
                    f"{self._ready_timeout:g}s of its start"
                )
            await asyncio.sleep(min(self._poll_interval, remaining))

    async def stop(self, instance_id: str) -> None:
        """结束实例的整个进程组：先 ``SIGTERM``，宽限期过后 ``SIGKILL``。

        Raises:
            RuntimeError: ``SIGKILL`` 之后进程仍未退出；实例仍记录在案，可以再次停止。
        """
        state = self._by_id.get(instance_id)
        if state is None:
            return
        await self._terminate(state)
        self._forget(state)

    async def is_running(self, instance_id: str) -> bool:
        state = self._by_id.get(instance_id)
        return state is not None and state.process.returncode is None

    async def aclose(self) -> None:
        """停止全部实例；之后 :meth:`launch` 抛出 :class:`RuntimeError`。"""
        self._closed = True
        states = list(self._by_id.values())
        results = await asyncio.gather(
            *(self._terminate(state) for state in states), return_exceptions=True
        )
        for state, result in zip(states, results, strict=True):
            self._forget(state)
            if isinstance(result, BaseException):
                logger.error(
                    "vLLM instance %s (pid %d) could not be stopped: %r",
                    state.instance.instance_id,
                    state.process.pid,
                    result,
                )

    def _cuda_devices(self, request: LaunchRequest) -> list[str]:
        provider = request.spec.runtime.provider
        if provider != PROVIDER:
            raise ValueError(
                f"runtime provider {provider!r} is not served by the vllm-process launcher "
                f"(it serves {PROVIDER!r})"
            )
        if not request.spec.model.reference.strip():
            raise ValueError("spec.model.reference must name the model to serve")
        foreign = sorted({device.node_id for device in request.devices} - {self._node_id})
        if foreign:
            raise ValueError(
                f"devices on node(s) {', '.join(foreign)} cannot be used: the vllm-process "
                f"launcher runs instances on node {self._node_id!r} only"
            )
        return [cuda_device(device.device_id) for device in request.devices]

    def _free_port(self) -> int:
        """操作系统挑选的空闲端口，避开本启动器的实例已经占用（或即将监听）的端口。"""
        taken = {state.port for state in self._by_id.values()}
        for _ in range(_PORT_ATTEMPTS):
            with socket.socket(socket_family(self._bind_host), socket.SOCK_STREAM) as sock:
                sock.bind((self._bind_host, 0))
                port = sock.getsockname()[1]
            if port not in taken:
                return port
        raise RuntimeError(f"no free port on {self._bind_host}")

    def _forget(self, state: _Process) -> None:
        self._by_id.pop(state.instance.instance_id, None)
        if self._by_key.get(state.key) is state:
            del self._by_key[state.key]

    async def _terminate(self, state: _Process) -> None:
        process = state.process
        if process.returncode is None:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(self._stop_grace):
                    await process.wait()
            except TimeoutError:
                logger.warning(
                    "vLLM instance %s (pid %d) did not exit within %gs of SIGTERM; killing it",
                    state.instance.instance_id,
                    process.pid,
                    self._stop_grace,
                )
        # 进程组中可能还有 vLLM 派生的工作进程（领头进程已退出也一样）。
        _signal_group(process.pid, signal.SIGKILL)
        try:
            async with asyncio.timeout(KILL_WAIT_SECONDS):
                await process.wait()
        except TimeoutError:
            raise RuntimeError(
                f"instance {state.instance.instance_id} (pid {process.pid}) did not exit "
                "after SIGKILL"
            ) from None
        logger.info(
            "vLLM instance stopped: instance_id=%s pid=%d returncode=%s",
            state.instance.instance_id,
            process.pid,
            process.returncode,
        )


__all__ = [
    "DEFAULT_BIND_HOST",
    "DEFAULT_COMMAND",
    "DEFAULT_READY_TIMEOUT_SECONDS",
    "DEFAULT_STOP_GRACE_SECONDS",
    "KILL_WAIT_SECONDS",
    "PROVIDER",
    "VLLM_MODULE",
    "VLLMProcessLauncher",
    "cuda_device",
]
