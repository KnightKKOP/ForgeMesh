"""进程启动器的实例记录测试：控制平面被 ``SIGKILL`` 之后，下次启动时结束它留下的实例进程。

* 记录文件在数据库旁；运行中的实例都有记录（pid、启动时间、私有目录，以及本机的 boot id
  与 PID 命名空间），停止之后删去；
* ``start`` 结束被杀死的控制平面留下的实例（vLLM 与 TGI）：实例进程、它放在别的进程组中且
  忽略 ``SIGTERM`` 的进程、私有目录与记录都不留下；
* 只向 pid 与启动时间都与记录一致的进程发送信号，另一次开机、另一个 PID 命名空间的记录被
  忽略；只删除名称属于实例的目录；
* 结束不了的实例保留记录，下次启动时再试；关闭时没能停止的实例保留记录；无法写入记录时
  启动失败、进程随即停止；无法读取的文件被忽略并在下次写入时覆盖；没有 ``/proc`` 时不记录。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from forgemesh.controlplane.deployment import (
    LaunchRequest,
    TGIProcessLauncher,
    VLLMProcessLauncher,
    deployment_run_key,
    process_records,
    record_file_path,
)
from forgemesh.controlplane.deployment import process as process_module
from forgemesh.controlplane.deployment.process import ProcessLauncher
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

HERE = Path(__file__).parent
HOST = HERE / "launcher_host.py"
STANDINS = {
    VLLMProcessLauncher: HERE / "vllm_standin.py",
    TGIProcessLauncher: HERE / "tgi_standin.py",
}
PROVIDERS = {VLLMProcessLauncher: "vllm", TGIProcessLauncher: "tgi"}


@pytest.fixture(autouse=True)
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """实例的私有目录建在这里：路径要短，Unix 套接字的路径至多 107 字节。

    依赖 ``tmp_path``：pytest 的临时目录须在替换 ``tempfile.tempdir`` 之前建好。
    """
    root = Path(tempfile.mkdtemp(prefix="fm-"))
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _identity() -> tuple[str, str]:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    return boot_id, os.readlink("/proc/self/ns/pid")


def _start_time(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19])


def _gone(pid: int) -> bool:
    """进程已不存在，或已结束、只等父进程回收（僵尸）。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return True
    return stat.rpartition(")")[2].split()[0] in {"Z", "X"}


def _processes_with_argument(argument: str) -> list[int]:
    """命令行中有 ``argument`` 的进程（僵尸进程的命令行为空）。"""
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if argument.encode() in (entry / "cmdline").read_bytes().split(b"\0"):
                    pids.append(int(entry.name))
    return pids


def _content(record_file: Path) -> dict[str, Any]:
    return json.loads(record_file.read_text(encoding="utf-8"))


def _entry(
    pid: int,
    *,
    instance_id: str = "vllm-0123456789ab",
    start_time: int | None = None,
    directories: Sequence[Path] = (),
) -> dict[str, Any]:
    return {
        "instanceId": instance_id,
        "instanceKey": deployment_run_key("uid-old", 1),
        "pid": pid,
        "startTime": _start_time(pid) if start_time is None else start_time,
        "directories": [str(path) for path in directories],
    }


def _write_records(
    record_file: Path, instances: list[dict[str, Any]], **changes: Any
) -> dict[str, Any]:
    boot_id, pid_namespace = _identity()
    content = {
        "version": 1,
        "bootId": boot_id,
        "pidNamespace": pid_namespace,
        "instances": instances,
        **changes,
    }
    record_file.write_text(json.dumps(content), encoding="utf-8")
    return content


@contextlib.contextmanager
def _session_process() -> Iterator[subprocess.Popen[bytes]]:
    """像实例进程那样在自己的会话中运行的进程；离开时杀掉并回收它。"""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True
    )
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def _request(provider: str, uid: str = "uid-1") -> LaunchRequest:
    return LaunchRequest(
        instance_key=deployment_run_key(uid, 1),
        tenant_id="tenant-1",
        resource=ResourceRef(kind="InferenceService", namespace="team-a", name="svc", uid=uid),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider=provider),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=16)
            ),
        ),
        allocation_id="alloc-1",
        devices=(AllocatedDevice(node_id="node-a", device_id="gpu-0"),),
    )


def _command(launcher_class: type[ProcessLauncher], report: Path, *args: str) -> list[str]:
    return [sys.executable, str(STANDINS[launcher_class]), "--report", str(report), *args]


def _launcher(
    launcher_class: type[ProcessLauncher], tmp_path: Path, record_file: Path, **options: Any
) -> tuple[ProcessLauncher, Path]:
    """以替身为命令、记录在 ``record_file`` 中的启动器，以及替身的启动报告文件。"""
    report = tmp_path / "starts.jsonl"
    options.setdefault("ready_timeout", 10.0)
    options.setdefault("stop_grace", 2.0)
    options.setdefault("poll_interval", 0.02)
    launcher = launcher_class(
        "node-a", command=_command(launcher_class, report), record_file=str(record_file), **options
    )
    return launcher, report


def _starts(report: Path) -> list[dict[str, Any]]:
    if not report.exists():
        return []
    return [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]


def _idle_launcher(record_file: Path, **options: Any) -> VLLMProcessLauncher:
    """只用来调用 ``start`` 的启动器（不启动实例）。"""
    return VLLMProcessLauncher(
        "node-a", command=[sys.executable], record_file=str(record_file), **options
    )


# ---------------------------------------------------------------------- #
# 记录
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("db_path", "expected"),
    [
        ("/data/forgemesh.db", "/data/forgemesh.instances.json"),
        ("/data/control-plane", "/data/control-plane.instances.json"),
        (":memory:", None),
    ],
)
def test_the_record_file_is_kept_next_to_the_database(db_path: str, expected: str | None) -> None:
    assert record_file_path(db_path) == expected


@pytest.mark.unit
def test_a_running_instance_is_recorded_until_it_is_stopped(tmp_path: Path) -> None:
    record_file = tmp_path / "cp.instances.json"
    launcher, report = _launcher(TGIProcessLauncher, tmp_path, record_file)

    async def scenario() -> tuple[str, dict[str, Any], int, dict[str, Any]]:
        instance = await launcher.launch(_request("tgi"))
        await launcher.verify(instance)
        recorded = _content(record_file)
        start_time = _start_time(_starts(report)[0]["pid"])
        await launcher.stop(instance.instance_id)
        return instance.instance_id, recorded, start_time, _content(record_file)

    instance_id, recorded, start_time, after = asyncio.run(scenario())
    [start] = _starts(report)
    boot_id, pid_namespace = _identity()
    assert recorded == {
        "version": 1,
        "bootId": boot_id,
        "pidNamespace": pid_namespace,
        "instances": [
            {
                "instanceId": instance_id,
                "instanceKey": deployment_run_key("uid-1", 1),
                "pid": start["pid"],
                "startTime": start_time,
                "directories": [str(Path(start["shard_uds_path"]).parent)],
            }
        ],
    }
    assert after == {**recorded, "instances": []}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cp.instances.json", "starts.jsonl"]


# ---------------------------------------------------------------------- #
# 启动时结束留下的实例
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("launcher_class", "standin_args"),
    [
        pytest.param(VLLMProcessLauncher, ("--spawn-child", "--child-group"), id="vllm-process"),
        pytest.param(TGIProcessLauncher, ("--spawn-child",), id="tgi-process"),
    ],
)
def test_start_stops_the_instances_a_killed_control_plane_left_running(
    tmp_path: Path,
    temp_root: Path,
    launcher_class: type[ProcessLauncher],
    standin_args: tuple[str, ...],
) -> None:
    record_file = tmp_path / "cp.instances.json"
    report = tmp_path / "starts.jsonl"
    ready = tmp_path / "ready"
    command = _command(launcher_class, report, *standin_args)
    host = subprocess.Popen(
        [
            sys.executable,
            str(HOST),
            launcher_class.__name__,
            str(record_file),
            _request(PROVIDERS[launcher_class]).model_dump_json(),
            json.dumps(command),
            str(ready),
        ],
        env={**os.environ, "TMPDIR": str(temp_root)},
    )
    try:
        deadline = time.monotonic() + 15.0
        while not ready.exists():
            assert host.poll() is None, "the control plane stand-in exited before it was ready"
            assert time.monotonic() < deadline, "the instance of the stand-in did not get ready"
            time.sleep(0.02)
        host.kill()
        host.wait()
        [start] = _starts(report)
        assert start["child_pgid"] != start["pid"]
        assert not _gone(start["pid"])
        assert not _gone(start["child_pid"])
        assert len(_content(record_file)["instances"]) == 1

        launcher = launcher_class(
            "node-a", command=command, record_file=str(record_file), stop_grace=0.2
        )
        asyncio.run(launcher.start())

        assert _gone(start["pid"])
        assert _gone(start["child_pid"])
        assert _content(record_file)["instances"] == []
        assert list(temp_root.iterdir()) == []
    finally:
        host.kill()
        host.wait()
        for start in _starts(report):
            for pid in (start["pid"], start["child_pid"]):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("start_time_offset", "file_changes", "stopped"),
    [
        pytest.param(0, {}, True, id="the-recorded-process"),
        pytest.param(1, {}, False, id="another-process-with-the-recorded-pid"),
        pytest.param(
            0, {"bootId": "00000000-0000-0000-0000-000000000000"}, False, id="another-boot"
        ),
        pytest.param(0, {"pidNamespace": "pid:[0]"}, False, id="another-pid-namespace"),
    ],
)
def test_start_signals_only_the_process_its_record_describes(
    tmp_path: Path, start_time_offset: int, file_changes: dict[str, str], stopped: bool
) -> None:
    record_file = tmp_path / "cp.instances.json"
    with _session_process() as process:
        entry = _entry(process.pid, start_time=_start_time(process.pid) + start_time_offset)
        content = _write_records(record_file, [entry], **file_changes)

        asyncio.run(_idle_launcher(record_file).start())

        assert _gone(process.pid) is stopped
    if file_changes:
        assert _content(record_file) == content
    else:
        assert _content(record_file) == {**content, "instances": []}


@pytest.mark.unit
def test_start_removes_only_the_directories_named_after_the_recorded_instance(
    tmp_path: Path,
) -> None:
    record_file = tmp_path / "cp.instances.json"
    owned = Path(tempfile.mkdtemp(prefix="forgemesh-tgi-0123456789ab-"))
    foreign = tmp_path / "models"
    foreign.mkdir()
    with _session_process() as process:
        entry = _entry(process.pid, instance_id="tgi-0123456789ab", directories=[owned, foreign])
    _write_records(record_file, [entry])

    asyncio.run(_idle_launcher(record_file).start())

    assert not owned.exists()
    assert foreign.exists()
    assert _content(record_file)["instances"] == []


@pytest.mark.unit
def test_a_leftover_that_cannot_be_stopped_keeps_its_record_until_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    record_file = tmp_path / "cp.instances.json"
    owned = Path(tempfile.mkdtemp(prefix="forgemesh-vllm-0123456789ab-"))
    with _session_process() as process:
        content = _write_records(record_file, [_entry(process.pid, directories=[owned])])
        with monkeypatch.context() as patch:
            patch.setattr(process_module, "_signal_group", lambda pgid, sig: None)
            patch.setattr(process_module, "KILL_WAIT_SECONDS", 0.2)
            asyncio.run(_idle_launcher(record_file, stop_grace=0.1).start())

        assert not _gone(process.pid)
        assert _content(record_file) == content
        assert owned.exists()
        assert "could not be stopped; it is stopped again at the next start" in caplog.text

        asyncio.run(_idle_launcher(record_file).start())

        assert _gone(process.pid)
    assert _content(record_file)["instances"] == []
    assert not owned.exists()


# ---------------------------------------------------------------------- #
# 写入失败、关闭失败、无法读取的文件与没有 /proc 的主机
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_launch_fails_and_stops_the_process_when_its_record_cannot_be_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """没写成的记录不算数：停止进程时不再试图删去它。"""
    record_file = tmp_path / "missing" / "cp.instances.json"
    launcher, report = _launcher(VLLMProcessLauncher, tmp_path, record_file)

    async def scenario() -> list[int]:
        try:
            with pytest.raises(FileNotFoundError):
                await launcher.launch(_request("vllm"))
            return _processes_with_argument(str(report))
        finally:
            await launcher.aclose()

    assert asyncio.run(scenario()) == []
    assert not record_file.parent.exists()
    assert "could not update the instance record file" not in caplog.text


@pytest.mark.unit
def test_an_instance_aclose_could_not_stop_stays_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop`` 在进程确实结束之后才报告失败：测试不留下进程，只看记录。"""
    record_file = tmp_path / "cp.instances.json"
    launcher, _ = _launcher(VLLMProcessLauncher, tmp_path, record_file)
    end_session = process_module._end_session

    async def end_session_then_fail(*args: Any) -> None:
        await end_session(*args)
        raise RuntimeError("the instance did not exit after SIGKILL")

    async def scenario() -> str:
        instance = await launcher.launch(_request("vllm"))
        await launcher.verify(instance)
        monkeypatch.setattr(process_module, "_end_session", end_session_then_fail)
        await launcher.aclose()
        return instance.instance_id

    instance_id = asyncio.run(scenario())
    assert [entry["instanceId"] for entry in _content(record_file)["instances"]] == [instance_id]


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param('{"version": 2, "instances": []}', id="another-version"),
        pytest.param(None, id="an-invalid-record"),
    ],
)
def test_an_unreadable_record_file_is_ignored_and_replaced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str | None
) -> None:
    record_file = tmp_path / "cp.instances.json"
    if content is None:
        _write_records(record_file, [{**_entry(os.getpid()), "pid": str(os.getpid())}])
    else:
        record_file.write_text(content, encoding="utf-8")
    launcher, _ = _launcher(VLLMProcessLauncher, tmp_path, record_file)

    async def scenario() -> tuple[str, dict[str, Any]]:
        await launcher.start()
        instance = await launcher.launch(_request("vllm"))
        recorded = _content(record_file)
        await launcher.aclose()
        return instance.instance_id, recorded

    instance_id, recorded = asyncio.run(scenario())
    assert f"ignoring the instance record file {record_file}" in caplog.text
    assert [entry["instanceId"] for entry in recorded["instances"]] == [instance_id]


@pytest.mark.unit
def test_instances_are_not_recorded_on_a_host_without_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(process_records, "_host_identity", lambda: None)
    record_file = tmp_path / "cp.instances.json"
    launcher, _ = _launcher(VLLMProcessLauncher, tmp_path, record_file)

    async def scenario() -> None:
        await launcher.start()
        instance = await launcher.launch(_request("vllm"))
        await launcher.verify(instance)
        await launcher.aclose()

    asyncio.run(scenario())
    assert not record_file.exists()
    assert "the boot id and PID namespace cannot be read from /proc" in caplog.text
