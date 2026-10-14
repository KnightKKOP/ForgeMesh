"""本机进程启动器的公共部分：每个实例是控制平面所在主机上的一个子进程（及其派生的进程）。

:class:`ProcessLauncher` 负责启动、就绪检查与停止，子类给出运行时、协议与命令行参数：
:class:`~.vllm_process.VLLMProcessLauncher`（vLLM）与 :class:`~.tgi_process.TGIProcessLauncher`
（TGI）。实例继承控制平面的环境变量（去掉 ``FORGEMESH_*``），``CUDA_VISIBLE_DEVICES`` 取自
分配的设备，未设置 ``CUDA_DEVICE_ORDER`` 时设为 ``PCI_BUS_ID``，设备序号因此与
``nvidia-smi`` 一致；端口由操作系统挑选，同一启动器的实例不会分到同一个端口。实例的标准
输出与标准错误即控制平面的输出。``spec.runtime.version`` 与 ``spec.model.digest`` 都不
检查：运行的是 ``command`` 对应的运行时，模型由它按引用加载。

实例在哪里运行
--------------
实例是控制平面的子进程，只能运行在控制平面所在的主机上：加速器清单必须恰好有一个节点
（就是这台主机），分配的设备都属于该节点（``node_id``）。清单中的设备 id 必须能写进
``CUDA_VISIBLE_DEVICES``：CUDA 设备序号（``0``）、带 ``gpu-`` 前缀的序号（``gpu-0``），
或以 ``GPU-`` / ``MIG-`` 开头的 UUID（原样使用），见 :func:`cuda_device`。

就绪与停止
----------
``verify`` 每隔 ``poll_interval`` 秒请求一次 ``GET /health``，返回 ``200`` 即就绪；进程
退出、或进程启动 ``ready_timeout`` 秒后仍未就绪时抛出。每个实例在自己的会话中运行（会话
id 即实例进程的 pid）：``stop`` 向实例进程的进程组发送 ``SIGTERM``，等它退出至多
``stop_grace`` 秒，然后向会话中的每个进程组发送 ``SIGKILL``。运行时派生的进程可能比实例
进程活得更久，也可能在别的进程组中（TGI 的 router 与 shard 各在自己的进程组中）。在 Linux
上（读 ``/proc``）``stop`` 等到会话中的进程都已退出才返回；调用 ``setsid`` 离开了会话的
进程不在此列，没有 ``/proc`` 时只等实例进程。

控制平面重启
------------
``aclose`` 停止全部实例，容器中整个进程树随容器结束。重启之后启动器不认识重启之前的实例
（``is_running`` 返回 ``False``）。控制平面直接在主机上运行、被 ``SIGKILL`` 时，实例进程在
自己的会话中，不会随之结束：给出 ``record_file`` 时启动器把每个实例记在这个文件中，下次
启动时 :meth:`ProcessLauncher.start` 结束其中仍在运行的实例，保证与限制见
:mod:`.process_records`。以 systemd 服务（默认 ``KillMode=control-group``）运行时，控制
平面的主进程结束之后 systemd 还会结束服务 cgroup 中剩下的进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
import os
import re
import shutil
import signal
import socket
import tempfile
import uuid
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from forgemesh.resource.inference import EndpointProtocol

from .launcher import LaunchedInstance, LaunchRequest, RuntimeLauncher
from .network import instance_url, local_address, probe_health, resolve_hosts, socket_family
from .process_records import ProcessRecord, ProcessRecordFile, directory_prefix

#: 默认监听地址（同时也是默认写进实例地址的主机）。
DEFAULT_BIND_HOST = "127.0.0.1"
#: 进程启动后等它就绪的默认时限（秒），包括下载与加载模型。
DEFAULT_READY_TIMEOUT_SECONDS = 1200.0
#: ``SIGTERM`` 之后等进程退出的默认宽限期（秒）。
DEFAULT_STOP_GRACE_SECONDS = 10.0
#: ``SIGKILL`` 之后等实例的进程退出的时限（秒）；超过即 :meth:`~ProcessLauncher.stop` 失败。
KILL_WAIT_SECONDS = 10.0

_INDEX_DEVICE = re.compile(r"(?:gpu-)?([0-9]+)")
_UUID_DEVICE = re.compile(r"(?:GPU|MIG)-[0-9A-Za-z][0-9A-Za-z/-]*")
_PORT_ATTEMPTS = 20
_KILL_POLL_SECONDS = 0.05
_ENDED_STATES = (b"Z", b"X")


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


def _stat_fields(pid: int) -> list[bytes] | None:
    """``/proc/<pid>/stat`` 中进程名之后的各项（从第 3 项状态开始）；读不到时为 ``None``。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as file:
            stat = file.read()
    except OSError:
        return None
    # 进程名在括号中且可能含空格与括号。
    return stat.rpartition(b")")[2].split()


def _start_time(pid: int) -> int | None:
    """进程的启动时间（开机以来的时钟滴答数，第 22 项）；读不到时为 ``None``。"""
    fields = _stat_fields(pid)
    return int(fields[19]) if fields is not None and len(fields) >= 20 else None


def _runs(pid: int, start_time: int) -> bool:
    """``pid`` 仍是启动时间为 ``start_time`` 的进程，而且尚未结束（僵尸进程不算）。"""
    fields = _stat_fields(pid)
    return (
        fields is not None
        and len(fields) >= 20
        and fields[0] not in _ENDED_STATES
        and int(fields[19]) == start_time
    )


def _session_groups(sid: int) -> set[int]:
    """会话 ``sid`` 中尚未结束的进程（僵尸进程不算）所在的进程组；没有 ``/proc`` 时为空。"""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return set()
    groups: set[int] = set()
    for entry in entries:
        fields = _stat_fields(int(entry)) if entry.isdigit() else None
        # 前四项依次是状态、父进程、进程组与会话。
        if (
            fields is not None
            and len(fields) >= 4
            and int(fields[3]) == sid
            and fields[0] not in _ENDED_STATES
        ):
            groups.add(int(fields[2]))
    return groups


async def _end_session(
    sid: int,
    leader_running: Callable[[], bool],
    wait: Callable[[float], Awaitable[object]],
    name: str,
) -> None:
    """向会话 ``sid`` 中的每个进程组发送 ``SIGKILL``，直到首进程与会话中的进程都已退出。

    Args:
        sid: 会话 id，即首进程（实例进程）的 pid。
        leader_running: 首进程是否仍在运行；只在它运行时向 ``sid`` 这个进程组发送信号。
        wait: 至多等待给定的秒数（首进程退出时可以提前返回）。
        name: 错误信息中的实例。

    Raises:
        RuntimeError: :data:`KILL_WAIT_SECONDS` 秒之后仍有进程没有退出。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + KILL_WAIT_SECONDS
    while True:
        groups = await asyncio.to_thread(_session_groups, sid)
        running = leader_running()
        for pgid in groups | ({sid} if running else set()):
            _signal_group(pgid, signal.SIGKILL)
        if not running and not groups:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            detail = (
                f"(pid {sid})"
                if running
                else f"(process groups {', '.join(map(str, sorted(groups)))})"
            )
            raise RuntimeError(f"{name} {detail} did not exit after SIGKILL")
        await wait(min(_KILL_POLL_SECONDS, remaining))


async def _wait_for_exit(process: asyncio.subprocess.Process, timeout: float) -> None:
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout):
            await process.wait()


class LaunchResources:
    """一次启动为实例预留的空闲端口与私有临时目录；实例停止之后这些目录被删除。"""

    def __init__(self, bind_host: str, taken: set[int], prefix: str) -> None:
        self._bind_host = bind_host
        self._taken = taken
        self._prefix = prefix
        self.ports: list[int] = []
        self.directories: list[str] = []

    def port(self) -> int:
        """操作系统挑选的空闲端口，避开本启动器的实例已经占用（或即将监听）的端口。

        Raises:
            RuntimeError: 找不到这样的端口。
        """
        for _ in range(_PORT_ATTEMPTS):
            with socket.socket(socket_family(self._bind_host), socket.SOCK_STREAM) as sock:
                sock.bind((self._bind_host, 0))
                port = sock.getsockname()[1]
            if port not in self._taken:
                self._taken.add(port)
                self.ports.append(port)
                return port
        raise RuntimeError(f"no free port on {self._bind_host}")

    def directory(self) -> str:
        """新建的私有临时目录（权限 ``0700``，名称以 ``forgemesh-<实例 id>-`` 开头）。"""
        path = tempfile.mkdtemp(prefix=self._prefix)
        self.directories.append(path)
        return path

    def remove_directories(self) -> None:
        for path in self.directories:
            shutil.rmtree(path, ignore_errors=True)


@dataclass(eq=False)
class _Process:
    key: str
    instance: LaunchedInstance
    process: asyncio.subprocess.Process
    port: int
    ready_deadline: float
    resources: LaunchResources


class ProcessLauncher(RuntimeLauncher):
    """把每个实例作为本机子进程运行的启动器（见模块说明）。

    子类设置下面的类属性并实现 :meth:`_arguments`。

    Args:
        node_id: 控制平面所在主机在加速器清单中的节点 id；分配到其他节点的设备无法使用。
        command: 启动运行时服务的命令（参数列表），默认 :attr:`default_command`。
        bind_host: 实例监听的 IP 地址，默认 ``127.0.0.1``；``0.0.0.0`` / ``::`` 监听全部地址。
        advertise_host: 写进实例地址的主机名或 IP 地址，默认同 ``bind_host``；监听全部
            地址时必须给出。
        ready_timeout: 进程启动后等它就绪的时限（秒）。
        stop_grace: ``SIGTERM`` 之后等进程退出的宽限期（秒），之后 ``SIGKILL``。
        poll_interval: 等待就绪时两次 ``GET /health`` 之间的秒数。
        record_file: 实例记录文件（见 :mod:`.process_records`）；为空时不记录实例，
            :meth:`start` 什么都不做。

    Raises:
        ValueError: 命令为空、地址非法或时间不是正数。
    """

    #: 日志与错误信息中的运行时名称，如 ``vLLM``。
    runtime_name: ClassVar[str]
    #: 启动器的名称（``FORGEMESH_RUNTIME_LAUNCHER`` 的取值），如 ``vllm-process``。
    launcher_name: ClassVar[str]
    #: 服务的运行时（``spec.runtime.provider``），也是实例 id 的前缀。
    provider: ClassVar[str]
    #: 实例提供的协议。
    protocol: ClassVar[EndpointProtocol]
    #: 默认的启动命令。
    default_command: ClassVar[tuple[str, ...]]

    def __init__(
        self,
        node_id: str,
        *,
        command: Sequence[str] | None = None,
        bind_host: str = DEFAULT_BIND_HOST,
        advertise_host: str | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        stop_grace: float = DEFAULT_STOP_GRACE_SECONDS,
        poll_interval: float = 1.0,
        record_file: str | None = None,
    ) -> None:
        if command is None:
            command = self.default_command
        if not node_id:
            raise ValueError("node_id must not be empty")
        if isinstance(command, str):
            raise ValueError(
                f"the {self.runtime_name} command must be a sequence of arguments, not a string"
            )
        if not command or not command[0]:
            raise ValueError(f"the {self.runtime_name} command must not be empty")
        self._logger = logging.getLogger(type(self).__module__)
        self._node_id = node_id
        self._command = tuple(command)
        self._bind_host, self._advertise_host = resolve_hosts(bind_host, advertise_host)
        self._probe_host = local_address(self._bind_host)
        self._ready_timeout = _positive("ready_timeout", ready_timeout)
        self._stop_grace = _positive("stop_grace", stop_grace)
        self._poll_interval = _positive("poll_interval", poll_interval)
        self._records = ProcessRecordFile(record_file) if record_file is not None else None
        self._by_key: dict[str, _Process] = {}
        self._by_id: dict[str, _Process] = {}
        self._closed = False

    @property
    def node_id(self) -> str:
        """实例运行所在的清单节点。"""
        return self._node_id

    @property
    def command(self) -> tuple[str, ...]:
        """启动运行时服务的命令。"""
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

    @abstractmethod
    def _arguments(
        self, request: LaunchRequest, devices: Sequence[str], port: int, resources: LaunchResources
    ) -> list[str]:
        """追加在命令之后的参数。

        Args:
            request: 启动请求。
            devices: 实例的 ``CUDA_VISIBLE_DEVICES``（与分配的设备一一对应）。
            port: 实例监听的端口。
            resources: 需要时从中再取空闲端口或私有临时目录。
        """

    async def start(self) -> None:
        """结束控制平面上次运行时启动、仍在运行的实例（见 :mod:`.process_records`）。

        结束不了的实例记一条错误日志并保留记录，下次启动时再试。
        """
        if self._records is None:
            return
        if not self._records.enabled:
            self._logger.warning(
                "instances are not recorded in %s: the boot id and PID namespace cannot be read "
                "from /proc, so instances left running by a killed control plane are not "
                "stopped at startup",
                self._records.path,
            )
            return
        leftovers = self._records.leftovers()
        results = await asyncio.gather(
            *(self._stop_leftover(record) for record in leftovers), return_exceptions=True
        )
        for record, result in zip(leftovers, results, strict=True):
            if isinstance(result, BaseException):
                self._logger.error(
                    "instance %s (pid %d), left running before the control plane restarted, "
                    "could not be stopped; it is stopped again at the next start: %r",
                    record.instance_id,
                    record.pid,
                    result,
                )
                continue
            if result:
                self._logger.warning(
                    "stopped instance %s (pid %d, instance_key=%s), left running before the "
                    "control plane restarted",
                    record.instance_id,
                    record.pid,
                    record.instance_key,
                )
            record.remove_directories()
            self._records.remove(record.instance_id)

    async def launch(self, request: LaunchRequest) -> LaunchedInstance:
        """启动实例进程并立即返回；``instance_key`` 的进程仍在运行时返回它。

        Raises:
            ValueError: 资源不是本启动器服务的运行时、模型引用为空、设备不在本节点或设备 id
                无法写进 ``CUDA_VISIBLE_DEVICES``。
            RuntimeError: 启动器已关闭，或找不到空闲端口。
            OSError: 无法启动命令，或无法写入实例记录（进程随即停止）。
        """
        if self._closed:
            raise RuntimeError(f"the {self.runtime_name} process launcher is closed")
        existing = self._by_key.get(request.instance_key)
        if existing is not None:
            if existing.process.returncode is None:
                return existing.instance
            await self._terminate(existing)
            self._forget(existing)
        devices = self._cuda_devices(request)
        instance_id = f"{self.provider}-{uuid.uuid4().hex[:12]}"
        resources = LaunchResources(
            self._bind_host,
            {port for state in self._by_id.values() for port in state.resources.ports},
            prefix=directory_prefix(instance_id),
        )
        env = {
            name: value for name, value in os.environ.items() if not name.startswith("FORGEMESH_")
        }
        env["CUDA_VISIBLE_DEVICES"] = ",".join(devices)
        env.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
        try:
            port = resources.port()
            instance = LaunchedInstance(
                instance_id=instance_id,
                url=instance_url(self._advertise_host, port),
                protocol=self.protocol,
            )
            arguments = self._arguments(request, devices, port, resources)
            process = await asyncio.create_subprocess_exec(
                *self._command,
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except BaseException:
            resources.remove_directories()
            raise
        state = _Process(
            key=request.instance_key,
            instance=instance,
            process=process,
            port=port,
            ready_deadline=asyncio.get_running_loop().time() + self._ready_timeout,
            resources=resources,
        )
        self._by_key[state.key] = state
        self._by_id[instance.instance_id] = state
        self._logger.info(
            "%s instance started: instance_id=%s pid=%d url=%s model=%s "
            "CUDA_VISIBLE_DEVICES=%s instance_key=%s",
            self.runtime_name,
            instance.instance_id,
            process.pid,
            instance.url,
            request.spec.model.reference,
            env["CUDA_VISIBLE_DEVICES"],
            state.key,
        )
        try:
            self._record(state)
        except OSError:
            await self.stop(instance.instance_id)
            raise
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
        """结束实例的全部进程：先 ``SIGTERM``，宽限期过后 ``SIGKILL``（见模块说明）。

        Raises:
            RuntimeError: ``SIGKILL`` 之后仍有进程没有退出；实例仍记录在案，可以再次停止。
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
            failed = isinstance(result, BaseException)
            self._forget(state, stopped=not failed)
            if failed:
                self._logger.error(
                    "%s instance %s (pid %d) could not be stopped: %r",
                    self.runtime_name,
                    state.instance.instance_id,
                    state.process.pid,
                    result,
                )

    def _cuda_devices(self, request: LaunchRequest) -> list[str]:
        provider = request.spec.runtime.provider
        if provider != self.provider:
            raise ValueError(
                f"runtime provider {provider!r} is not served by the {self.launcher_name} "
                f"launcher (it serves {self.provider!r})"
            )
        if not request.spec.model.reference.strip():
            raise ValueError("spec.model.reference must name the model to serve")
        foreign = sorted({device.node_id for device in request.devices} - {self._node_id})
        if foreign:
            raise ValueError(
                f"devices on node(s) {', '.join(foreign)} cannot be used: the "
                f"{self.launcher_name} launcher runs instances on node {self._node_id!r} only"
            )
        return [cuda_device(device.device_id) for device in request.devices]

    def _record(self, state: _Process) -> None:
        if self._records is None:
            return
        start_time = _start_time(state.process.pid)
        if start_time is None:
            return
        self._records.add(
            ProcessRecord(
                instance_id=state.instance.instance_id,
                instance_key=state.key,
                pid=state.process.pid,
                start_time=start_time,
                directories=tuple(state.resources.directories),
            )
        )

    def _forget(self, state: _Process, *, stopped: bool = True) -> None:
        """不再管理实例；``stopped`` 为假（没能停止）时保留它的记录，下次启动时再结束它。"""
        self._by_id.pop(state.instance.instance_id, None)
        if self._by_key.get(state.key) is state:
            del self._by_key[state.key]
        state.resources.remove_directories()
        if stopped and self._records is not None:
            self._records.remove(state.instance.instance_id)

    async def _terminate(self, state: _Process) -> None:
        process = state.process
        if process.returncode is None:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                async with asyncio.timeout(self._stop_grace):
                    await process.wait()
            except TimeoutError:
                self._logger.warning(
                    "%s instance %s (pid %d) did not exit within %gs of SIGTERM; killing it",
                    self.runtime_name,
                    state.instance.instance_id,
                    process.pid,
                    self._stop_grace,
                )
        # 实例进程已被回收时，它的进程组中可能还有进程，没有 /proc 时会话扫描找不到它们；
        # 进程组中还有进程时这个 id 不会被复用。
        _signal_group(process.pid, signal.SIGKILL)
        await _end_session(
            process.pid,
            lambda: process.returncode is None,
            functools.partial(_wait_for_exit, process),
            f"instance {state.instance.instance_id}",
        )
        self._logger.info(
            "%s instance stopped: instance_id=%s pid=%d returncode=%s",
            self.runtime_name,
            state.instance.instance_id,
            process.pid,
            process.returncode,
        )

    async def _stop_leftover(self, record: ProcessRecord) -> bool:
        """像 :meth:`stop` 那样结束记录中的进程；它已不在（或 pid 已被复用）时返回 ``False``。

        Raises:
            RuntimeError: ``SIGKILL`` 之后仍有进程没有退出。
        """

        def running() -> bool:
            return _runs(record.pid, record.start_time)

        if not running():
            return False
        _signal_group(record.pid, signal.SIGTERM)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_grace
        while running() and loop.time() < deadline:
            await asyncio.sleep(_KILL_POLL_SECONDS)
        await _end_session(record.pid, running, asyncio.sleep, f"instance {record.instance_id}")
        return True


__all__ = [
    "DEFAULT_BIND_HOST",
    "DEFAULT_READY_TIMEOUT_SECONDS",
    "DEFAULT_STOP_GRACE_SECONDS",
    "KILL_WAIT_SECONDS",
    "LaunchResources",
    "ProcessLauncher",
    "cuda_device",
]
