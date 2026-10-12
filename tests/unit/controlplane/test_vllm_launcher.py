"""vLLM 进程启动器测试：用替身（``vllm_standin.py``）代替 vLLM 运行，不需要 GPU。

* 启动命令带上模型、监听地址、空闲端口与张量并行数；``CUDA_VISIBLE_DEVICES`` 取自分配的
  设备；实例在自己的会话中运行，看不到 ``FORGEMESH_*`` 环境变量；
* 同一实例幂等键在进程运行期间返回同一实例，进程退出之后启动新进程；
* ``verify`` 等到 ``GET /health`` 返回 200，进程退出或超过就绪时限时失败，不认识的实例
  立即失败；
* ``stop`` 先 ``SIGTERM``、宽限期过后 ``SIGKILL``，结束整个进程组；``aclose`` 停止全部实例；
* 无法服务的请求（其他运行时、其他节点的设备、无法写进 ``CUDA_VISIBLE_DEVICES`` 的设备 id）
  不启动进程；非法配置在构造时被拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from forgemesh.controlplane.deployment import (
    LaunchedInstance,
    LaunchRequest,
    VLLMProcessLauncher,
    deployment_run_key,
)
from forgemesh.controlplane.deployment.vllm_process import cuda_device
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import (
    AcceleratorSpec,
    EndpointProtocol,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

STANDIN = Path(__file__).with_name("vllm_standin.py")
LOGGER = "forgemesh.controlplane.deployment.vllm_process"
GPU_UUID = "GPU-8f0c5a62-3b1e-4a7d-9c2f-0d1e2f3a4b5c"


def _request(
    uid: str = "uid-1",
    *,
    provider: str = "vllm",
    reference: str = "models/llama",
    devices: tuple[tuple[str, str], ...] = (("node-a", "gpu-0"),),
) -> LaunchRequest:
    return LaunchRequest(
        instance_key=deployment_run_key(uid),
        tenant_id="tenant-1",
        resource=ResourceRef(kind="InferenceService", namespace="team-a", name="svc", uid=uid),
        spec=InferenceServiceSpec(
            model=ModelRef(reference=reference),
            runtime=RuntimeRef(provider=provider),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(
                    type="nvidia-gpu", count=len(devices) or 1, minMemoryGiB=16
                )
            ),
        ),
        allocation_id="alloc-1",
        devices=tuple(AllocatedDevice(node_id=node, device_id=device) for node, device in devices),
    )


def _launcher(
    tmp_path: Path, *standin_args: str, **options: Any
) -> tuple[VLLMProcessLauncher, Path]:
    """以替身为命令的启动器，以及替身记录每次启动的报告文件。"""
    report = tmp_path / "starts.jsonl"
    options.setdefault("ready_timeout", 10.0)
    options.setdefault("stop_grace", 2.0)
    options.setdefault("poll_interval", 0.02)
    command = (sys.executable, str(STANDIN), "--report", str(report), *standin_args)
    return VLLMProcessLauncher("node-a", command=command, **options), report


def _starts(report: Path) -> list[dict[str, Any]]:
    if not report.exists():
        return []
    return [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]


def _port(instance: LaunchedInstance) -> int:
    port = urlsplit(instance.url).port
    assert port is not None
    return port


def _gone(pid: int) -> bool:
    """进程已不存在，或已结束、只等父进程回收（僵尸）。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rpartition(")")[2].split()[0] in {"Z", "X"}


async def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not _gone(pid):
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def _refused(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except ConnectionRefusedError:
        return True
    writer.close()
    await writer.wait_closed()
    return False


# ---------------------------------------------------------------------- #
# 启动
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_launch_runs_the_command_with_the_model_address_and_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEMESH_STANDIN_SECRET", "not-for-instances")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    launcher, report = _launcher(tmp_path)
    request = _request(devices=(("node-a", "gpu-1"), ("node-a", GPU_UUID)))

    async def scenario() -> tuple[LaunchedInstance, bool, bool]:
        instance = await launcher.launch(request)
        await launcher.verify(instance)
        running = await launcher.is_running(instance.instance_id)
        await launcher.aclose()
        return instance, running, await launcher.is_running(instance.instance_id)

    instance, running, after_close = asyncio.run(scenario())
    [start] = _starts(report)
    port = _port(instance)
    assert instance.instance_id.startswith("vllm-")
    assert instance.url == f"http://127.0.0.1:{port}"
    assert instance.protocol is EndpointProtocol.OPENAI_CHAT
    assert start["argv"][-7:] == [
        "--model=models/llama",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--tensor-parallel-size",
        "2",
    ]
    assert start["env"] == {
        "CUDA_VISIBLE_DEVICES": f"1,{GPU_UUID}",
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "FORGEMESH_STANDIN_SECRET": None,
    }
    assert start["sid"] == start["pid"]
    assert (running, after_close) == (True, False)
    assert _gone(start["pid"])


@pytest.mark.unit
def test_an_instance_listening_on_all_addresses_is_advertised_under_the_configured_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUDA_DEVICE_ORDER", "FASTEST_FIRST")
    launcher, report = _launcher(tmp_path, bind_host="0.0.0.0", advertise_host="vllm.internal")

    async def scenario() -> LaunchedInstance:
        instance = await launcher.launch(_request())
        await launcher.verify(instance)
        await launcher.aclose()
        return instance

    instance = asyncio.run(scenario())
    [start] = _starts(report)
    assert instance.url == f"http://vllm.internal:{start['port']}"
    assert start["host"] == "0.0.0.0"
    assert start["env"]["CUDA_DEVICE_ORDER"] == "FASTEST_FIRST"


@pytest.mark.unit
def test_launch_returns_the_running_instance_for_the_same_key(tmp_path: Path) -> None:
    launcher, report = _launcher(tmp_path)

    async def scenario() -> tuple[LaunchedInstance, LaunchedInstance, LaunchedInstance]:
        first = await launcher.launch(_request("uid-1"))
        again = await launcher.launch(_request("uid-1"))
        other = await launcher.launch(_request("uid-2"))
        await launcher.verify(first)
        await launcher.verify(other)
        await launcher.aclose()
        return first, again, other

    first, again, other = asyncio.run(scenario())
    assert again == first
    assert other.instance_id != first.instance_id
    assert _port(other) != _port(first)
    assert len(_starts(report)) == 2


@pytest.mark.unit
def test_launch_starts_a_new_process_once_the_previous_one_exited(tmp_path: Path) -> None:
    launcher, report = _launcher(tmp_path, "--mode", "exit")

    async def scenario() -> tuple[LaunchedInstance, LaunchedInstance, bool]:
        first = await launcher.launch(_request())
        with pytest.raises(
            RuntimeError,
            match=f"instance {first.instance_id} exited with code 3 before it became healthy",
        ):
            await launcher.verify(first)
        second = await launcher.launch(_request())
        running = await launcher.is_running(first.instance_id)
        with pytest.raises(RuntimeError, match=f"instance {second.instance_id} exited with code 3"):
            await launcher.verify(second)
        await launcher.aclose()
        return first, second, running

    first, second, running = asyncio.run(scenario())
    assert second.instance_id != first.instance_id
    assert running is False
    assert len(_starts(report)) == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("request_options", "message"),
    [
        pytest.param(
            {"provider": "tgi"},
            "runtime provider 'tgi' is not served by the vllm-process launcher (it serves 'vllm')",
            id="other-runtime",
        ),
        pytest.param({"provider": "vLLM"}, "runtime provider 'vLLM'", id="provider-spelling"),
        pytest.param(
            {"reference": "  "},
            "spec.model.reference must name the model to serve",
            id="blank-model",
        ),
        pytest.param(
            {"devices": (("node-a", "gpu-0"), ("node-b", "gpu-0"))},
            "devices on node(s) node-b cannot be used: the vllm-process launcher runs "
            "instances on node 'node-a' only",
            id="other-node",
        ),
        pytest.param(
            {"devices": (("node-a", "accel-0"),)},
            "device id 'accel-0' is neither a CUDA device index",
            id="device-id",
        ),
    ],
)
def test_launch_refuses_requests_it_cannot_serve_without_starting_a_process(
    tmp_path: Path, request_options: dict[str, Any], message: str
) -> None:
    launcher, report = _launcher(tmp_path)

    async def scenario() -> str:
        with pytest.raises(ValueError) as caught:
            await launcher.launch(_request(**request_options))
        await launcher.aclose()
        return str(caught.value)

    assert message in asyncio.run(scenario())
    assert _starts(report) == []


@pytest.mark.unit
def test_a_command_that_cannot_be_started_leaves_no_instance_behind(tmp_path: Path) -> None:
    launcher = VLLMProcessLauncher("node-a", command=[str(tmp_path / "no-such-vllm")])

    async def scenario() -> None:
        for _ in range(2):
            with pytest.raises(FileNotFoundError):
                await launcher.launch(_request())
        await launcher.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# 就绪
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_verify_waits_until_the_instance_answers_health(tmp_path: Path) -> None:
    launcher, _ = _launcher(tmp_path, "--delay", "0.3")

    async def scenario() -> float:
        instance = await launcher.launch(_request())
        started = time.monotonic()
        await launcher.verify(instance)
        waited = time.monotonic() - started
        await launcher.aclose()
        return waited

    assert asyncio.run(scenario()) >= 0.3


@pytest.mark.unit
def test_verify_fails_when_the_instance_does_not_become_healthy_in_time(tmp_path: Path) -> None:
    launcher, _ = _launcher(tmp_path, "--mode", "hang", ready_timeout=0.3)

    async def scenario() -> tuple[float, bool, bool]:
        instance = await launcher.launch(_request())
        with pytest.raises(
            RuntimeError,
            match=rf"instance {instance.instance_id} did not become healthy within 0\.3s of its "
            "start",
        ):
            await launcher.verify(instance)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="did not become healthy"):
            await launcher.verify(instance)
        retried = time.monotonic() - started
        running = await launcher.is_running(instance.instance_id)
        await launcher.stop(instance.instance_id)
        return retried, running, await launcher.is_running(instance.instance_id)

    retried, running, after_stop = asyncio.run(scenario())
    assert retried < 0.3
    assert (running, after_stop) == (True, False)


@pytest.mark.unit
def test_an_unknown_instance_is_not_running_and_fails_verification_at_once(
    tmp_path: Path,
) -> None:
    launcher, _ = _launcher(tmp_path)
    instance = LaunchedInstance(
        instance_id="vllm-gone", url="http://127.0.0.1:9", protocol=EndpointProtocol.OPENAI_CHAT
    )

    async def scenario() -> bool:
        running = await launcher.is_running("vllm-gone")
        with pytest.raises(RuntimeError, match="instance vllm-gone is not running"):
            await launcher.verify(instance)
        await launcher.stop("vllm-gone")
        return running

    assert asyncio.run(scenario()) is False


# ---------------------------------------------------------------------- #
# 停止
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_stop_kills_an_instance_that_ignores_sigterm_after_the_grace_period(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    launcher, report = _launcher(tmp_path, "--ignore-sigterm", stop_grace=0.2)

    async def scenario() -> tuple[LaunchedInstance, bool, bool]:
        instance = await launcher.launch(_request())
        await launcher.verify(instance)
        await launcher.stop(instance.instance_id)
        running = await launcher.is_running(instance.instance_id)
        return instance, running, await _refused(_port(instance))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        instance, running, refused = asyncio.run(scenario())
    [start] = _starts(report)
    assert (running, refused) == (False, True)
    assert _gone(start["pid"])
    assert (
        f"vLLM instance {instance.instance_id} (pid {start['pid']}) did not exit within 0.2s of "
        "SIGTERM; killing it"
    ) in caplog.text


@pytest.mark.unit
def test_stop_ends_every_process_of_the_instance(tmp_path: Path) -> None:
    launcher, report = _launcher(tmp_path, "--spawn-child")

    async def scenario() -> bool:
        instance = await launcher.launch(_request())
        await launcher.verify(instance)
        await launcher.stop(instance.instance_id)
        return await _wait_gone(_starts(report)[0]["child_pid"])

    try:
        assert asyncio.run(scenario())
    finally:
        for start in _starts(report):
            with contextlib.suppress(ProcessLookupError):
                os.kill(start["child_pid"], signal.SIGKILL)


@pytest.mark.unit
def test_aclose_stops_every_instance_and_refuses_new_launches(tmp_path: Path) -> None:
    launcher, report = _launcher(tmp_path)

    async def scenario() -> tuple[list[bool], list[bool]]:
        instances = [await launcher.launch(_request(uid)) for uid in ("uid-1", "uid-2")]
        for instance in instances:
            await launcher.verify(instance)
        await launcher.aclose()
        running = [await launcher.is_running(instance.instance_id) for instance in instances]
        refused = [await _refused(_port(instance)) for instance in instances]
        with pytest.raises(RuntimeError, match="the vLLM process launcher is closed"):
            await launcher.launch(_request("uid-3"))
        return running, refused

    running, refused = asyncio.run(scenario())
    starts = _starts(report)
    assert (running, refused) == ([False, False], [True, True])
    assert len(starts) == 2
    assert all(_gone(start["pid"]) for start in starts)


# ---------------------------------------------------------------------- #
# 设备 id 与配置
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("device_id", "expected"),
    [
        ("0", "0"),
        ("gpu-3", "3"),
        ("gpu-010", "10"),
        (GPU_UUID, GPU_UUID),
        ("MIG-5c6a3b4d-1e2f-4a5b-8c9d-0e1f2a3b4c5d", "MIG-5c6a3b4d-1e2f-4a5b-8c9d-0e1f2a3b4c5d"),
        (f"MIG-{GPU_UUID}/1/0", f"MIG-{GPU_UUID}/1/0"),
    ],
)
def test_cuda_device_maps_inventory_device_ids(device_id: str, expected: str) -> None:
    assert cuda_device(device_id) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "device_id", ["", "gpu-", "gpu0", "GPU-", "mig-0", "nvidia0", "0,1", " 0", "gpu-1 ", "\u0663"]
)
def test_cuda_device_refuses_ids_cuda_cannot_select(device_id: str) -> None:
    with pytest.raises(ValueError, match="is neither a CUDA device index"):
        cuda_device(device_id)


@pytest.mark.unit
def test_the_default_command_runs_vllm_with_the_control_plane_interpreter() -> None:
    launcher = VLLMProcessLauncher("node-a")

    assert launcher.command == (sys.executable, "-m", "vllm.entrypoints.openai.api_server")
    assert launcher.node_id == "node-a"
    assert (launcher.bind_host, launcher.advertise_host) == ("127.0.0.1", "127.0.0.1")
    assert (launcher.ready_timeout, launcher.stop_grace) == (1200.0, 10.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"node_id": ""}, "node_id must not be empty"),
        ({"command": ()}, "the vLLM command must not be empty"),
        ({"command": ("",)}, "the vLLM command must not be empty"),
        ({"command": "vllm serve"}, "must be a sequence of arguments, not a string"),
        ({"bind_host": "localhost"}, "'localhost' is not an IP address"),
        ({"bind_host": "::"}, "an advertise host is required when listening on ::"),
        ({"advertise_host": "0.0.0.0"}, "'0.0.0.0' is a wildcard address"),
        ({"advertise_host": "vllm_host"}, "is neither an IP address nor a host name"),
        ({"ready_timeout": 0}, "ready_timeout must be a positive number of seconds"),
        ({"stop_grace": -1.0}, "stop_grace must be a positive number of seconds"),
        ({"poll_interval": math.inf}, "poll_interval must be a positive number of seconds"),
        ({"ready_timeout": math.nan}, "ready_timeout must be a positive number of seconds"),
    ],
)
def test_invalid_settings_are_refused(options: dict[str, Any], message: str) -> None:
    settings: dict[str, Any] = {"node_id": "node-a", **options}

    with pytest.raises(ValueError) as caught:
        VLLMProcessLauncher(**settings)

    assert message in str(caught.value)
