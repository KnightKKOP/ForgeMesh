"""TGI 进程启动器测试：用替身（``tgi_standin.py``）代替 ``text-generation-launcher``，不需要 GPU。

* 启动命令带上模型、监听地址、空闲端口、shard 数、torch distributed 用的另一个空闲端口与
  实例私有目录（权限 ``0700``）中的 shard 套接字路径；``CUDA_VISIBLE_DEVICES`` 取自分配的
  设备；实例提供 ``tgi-generate`` 协议，``verify`` 等到 ``GET /health`` 返回 200；
* 同一启动器的实例不共用端口与套接字目录；实例停止、或退出之后重新启动时删除它的目录，
  命令无法启动时不留下目录；
* ``stop`` 结束 TGI 放在别的进程组中的进程；
* 其他运行时、其他节点的设备不启动进程；默认命令与非法配置。

启动、就绪与停止的其余行为与 vLLM 进程启动器共用，见 ``test_vllm_launcher.py``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from forgemesh.controlplane.deployment import (
    LaunchedInstance,
    LaunchRequest,
    TGIProcessLauncher,
    deployment_run_key,
)
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import (
    AcceleratorSpec,
    EndpointProtocol,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

STANDIN = Path(__file__).with_name("tgi_standin.py")
GPU_UUID = "GPU-8f0c5a62-3b1e-4a7d-9c2f-0d1e2f3a4b5c"


@pytest.fixture(autouse=True)
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """启动器在这里建实例的私有目录：路径要短，Unix 套接字的路径至多 107 字节。

    依赖 ``tmp_path``：pytest 的临时目录须在替换 ``tempfile.tempdir`` 之前建好。
    """
    root = Path(tempfile.mkdtemp(prefix="fm-"))
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _request(
    uid: str = "uid-1",
    *,
    provider: str = "tgi",
    devices: tuple[tuple[str, str], ...] = (("node-a", "gpu-0"),),
) -> LaunchRequest:
    return LaunchRequest(
        instance_key=deployment_run_key(uid, 1),
        tenant_id="tenant-1",
        resource=ResourceRef(kind="InferenceService", namespace="team-a", name="svc", uid=uid),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
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
) -> tuple[TGIProcessLauncher, Path]:
    """以替身为命令的启动器，以及替身记录每次启动的报告文件。"""
    report = tmp_path / "starts.jsonl"
    options.setdefault("ready_timeout", 10.0)
    options.setdefault("stop_grace", 2.0)
    options.setdefault("poll_interval", 0.02)
    command = (sys.executable, str(STANDIN), "--report", str(report), *standin_args)
    return TGIProcessLauncher("node-a", command=command, **options), report


def _starts(report: Path) -> list[dict[str, Any]]:
    if not report.exists():
        return []
    return [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]


def _gone(pid: int) -> bool:
    """进程已不存在，或已结束、只等父进程回收（僵尸）。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return True
    return stat.rpartition(")")[2].split()[0] in {"Z", "X"}


def _shard_directory(start: dict[str, Any]) -> Path:
    return Path(start["shard_uds_path"]).parent


# ---------------------------------------------------------------------- #
# 启动
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_launch_runs_tgi_with_the_model_address_shards_and_a_private_socket_directory(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEMESH_STANDIN_SECRET", "not-for-instances")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)
    launcher, report = _launcher(tmp_path)
    request = _request(devices=(("node-a", "gpu-1"), ("node-a", GPU_UUID)))

    async def scenario() -> tuple[LaunchedInstance, list[Path], bool]:
        instance = await launcher.launch(request)
        await launcher.verify(instance)
        directories = list(temp_root.iterdir())
        running = await launcher.is_running(instance.instance_id)
        await launcher.aclose()
        return instance, directories, running

    instance, directories, running = asyncio.run(scenario())
    [start] = _starts(report)
    directory = _shard_directory(start)
    assert instance.instance_id.startswith("tgi-")
    assert instance.url == f"http://127.0.0.1:{start['port']}"
    assert instance.protocol is EndpointProtocol.TGI_GENERATE
    assert start["argv"][-12:] == [
        "--model-id",
        "models/llama",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(start["port"]),
        "--num-shard",
        "2",
        "--master-port",
        str(start["master_port"]),
        "--shard-uds-path",
        str(directory / "shard"),
    ]
    assert start["master_port"] != start["port"]
    assert directories == [directory]
    assert directory.name.startswith(f"forgemesh-{instance.instance_id}-")
    assert start["shard_directory_mode"] == 0o700
    assert start["env"] == {
        "CUDA_VISIBLE_DEVICES": f"1,{GPU_UUID}",
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "FORGEMESH_STANDIN_SECRET": None,
    }
    assert start["sid"] == start["pid"]
    assert running is True
    assert not directory.exists()
    assert _gone(start["pid"])


@pytest.mark.unit
def test_instances_do_not_share_ports_or_socket_directories(
    tmp_path: Path, temp_root: Path
) -> None:
    launcher, report = _launcher(tmp_path)

    async def scenario() -> list[Path]:
        instances = [await launcher.launch(_request(uid)) for uid in ("uid-1", "uid-2")]
        for instance in instances:
            await launcher.verify(instance)
        directories = sorted(temp_root.iterdir())
        await launcher.aclose()
        with pytest.raises(RuntimeError, match="the TGI process launcher is closed"):
            await launcher.launch(_request("uid-3"))
        return directories

    directories = asyncio.run(scenario())
    first, second = _starts(report)
    assert len({first["port"], first["master_port"], second["port"], second["master_port"]}) == 4
    assert directories == sorted([_shard_directory(first), _shard_directory(second)])
    assert list(temp_root.iterdir()) == []


@pytest.mark.unit
def test_the_directory_of_an_instance_that_exited_is_removed_when_it_is_launched_again(
    tmp_path: Path, temp_root: Path
) -> None:
    launcher, report = _launcher(tmp_path, "--mode", "exit")

    async def scenario() -> tuple[list[Path], list[Path]]:
        first = await launcher.launch(_request())
        with pytest.raises(RuntimeError, match="exited with code 3 before it became healthy"):
            await launcher.verify(first)
        before = list(temp_root.iterdir())
        second = await launcher.launch(_request())
        with pytest.raises(RuntimeError, match="exited with code 3"):
            await launcher.verify(second)
        after = list(temp_root.iterdir())
        await launcher.stop(second.instance_id)
        return before, after

    before, after = asyncio.run(scenario())
    first, second = _starts(report)
    assert before == [_shard_directory(first)]
    assert after == [_shard_directory(second)]
    assert list(temp_root.iterdir()) == []


@pytest.mark.unit
def test_a_command_that_cannot_be_started_leaves_no_directory_behind(
    tmp_path: Path, temp_root: Path
) -> None:
    launcher = TGIProcessLauncher("node-a", command=[str(tmp_path / "no-such-tgi")])

    async def scenario() -> None:
        with pytest.raises(FileNotFoundError):
            await launcher.launch(_request())
        await launcher.aclose()

    asyncio.run(scenario())
    assert list(temp_root.iterdir()) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("request_options", "message"),
    [
        pytest.param(
            {"provider": "vllm"},
            "runtime provider 'vllm' is not served by the tgi-process launcher (it serves 'tgi')",
            id="other-runtime",
        ),
        pytest.param(
            {"devices": (("node-b", "gpu-0"),)},
            "devices on node(s) node-b cannot be used: the tgi-process launcher runs "
            "instances on node 'node-a' only",
            id="other-node",
        ),
    ],
)
def test_launch_refuses_requests_it_cannot_serve_without_starting_a_process(
    tmp_path: Path, temp_root: Path, request_options: dict[str, Any], message: str
) -> None:
    launcher, report = _launcher(tmp_path)

    async def scenario() -> str:
        with pytest.raises(ValueError) as caught:
            await launcher.launch(_request(**request_options))
        await launcher.aclose()
        return str(caught.value)

    assert message in asyncio.run(scenario())
    assert _starts(report) == []
    assert list(temp_root.iterdir()) == []


# ---------------------------------------------------------------------- #
# 停止
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_stop_ends_the_processes_tgi_runs_in_process_groups_of_their_own(
    tmp_path: Path, temp_root: Path
) -> None:
    launcher, report = _launcher(tmp_path, "--spawn-child")

    async def scenario() -> bool:
        instance = await launcher.launch(_request())
        await launcher.verify(instance)
        await launcher.stop(instance.instance_id)
        return await launcher.is_running(instance.instance_id)

    try:
        running = asyncio.run(scenario())
        [start] = _starts(report)
        assert start["child_pgid"] != start["pid"]
        assert running is False
        assert _gone(start["pid"])
        assert _gone(start["child_pid"])
        assert list(temp_root.iterdir()) == []
    finally:
        for start in _starts(report):
            with contextlib.suppress(ProcessLookupError):
                os.kill(start["child_pid"], signal.SIGKILL)


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_default_command_runs_text_generation_launcher() -> None:
    launcher = TGIProcessLauncher("node-a")

    assert launcher.command == ("text-generation-launcher",)
    assert launcher.node_id == "node-a"
    assert (launcher.bind_host, launcher.advertise_host) == ("127.0.0.1", "127.0.0.1")
    assert (launcher.ready_timeout, launcher.stop_grace) == (1200.0, 10.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"command": ()}, "the TGI command must not be empty"),
        (
            {"command": "text-generation-launcher --model-id gpt2"},
            "the TGI command must be a sequence of arguments, not a string",
        ),
        ({"stop_grace": 0}, "stop_grace must be a positive number of seconds"),
    ],
)
def test_invalid_settings_are_refused(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        TGIProcessLauncher("node-a", **options)

    assert message in str(caught.value)
