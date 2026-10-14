"""本机进程启动器的实例记录：控制平面被杀死之后，下次启动时结束它留下的实例进程。

控制平面直接在主机上运行、被 ``SIGKILL``（或因崩溃、OOM 而结束）时，实例进程在自己的会话
中，不会随之结束，继续占用 GPU。:class:`~.process.ProcessLauncher` 因此把它启动的每个实例
记在数据库旁的 JSON 文件中（:func:`record_file_path`，如 ``forgemesh.instances.json``）：
实例 id、实例键、进程 id（也是实例的进程组与会话 id）、进程的启动时间（``/proc/<pid>/stat``
中的 ``starttime``）与实例私有的临时目录，连同本机的 boot id 与控制平面所在的 PID 命名空间。
实例停止之后删去它的记录。无法写入记录时启动实例失败（已启动的进程随即停止）；无法删去
记录时只记一条警告：多余的记录无害，下次启动时发现进程已不在就丢弃它。

应用在恢复未完成的部署之前调用 :meth:`~.process.ProcessLauncher.start`：记录中仍在运行的
实例像 ``stop`` 那样结束（向进程组发送 ``SIGTERM``，宽限期过后向会话中的每个进程组发送
``SIGKILL``，等它们退出），然后删除实例的私有目录与记录。

保证与限制
----------
* 只向 pid 与启动时间都与记录一致的进程（及其会话中的进程）发送信号：pid 被别的进程复用时
  启动时间不同，那个进程不受影响。
* 记录只在同一次开机、同一个 PID 命名空间中有效：主机重启或容器重新创建之后，实例进程已
  随之结束，文件中的记录被忽略。
* 实例进程本身已经退出时，不再寻找它会话中剩下的进程：仅凭会话 id 无法确认那还是同一个
  会话。调用 ``setsid`` 离开了会话的进程也找不到。
* ``SIGKILL`` 之后 :data:`~.process.KILL_WAIT_SECONDS` 秒仍未退出的进程保留记录，下次启动时
  再试；启动照常继续。
* 只有选择了进程启动器（``vllm-process`` / ``tgi-process``）的控制平面在启动时处理记录；
  控制平面再次启动之前，留下的进程一直运行并占用 GPU。没有使用 ``PR_SET_PDEATHSIG``：
  它只作用于实例进程本身，TGI 放在别的进程组中的 router 与 shard 不受影响；Python 只能在
  ``preexec_fn`` 中设置它，而控制平面有线程（SQLite 调用在线程中执行），这时
  ``preexec_fn`` 并不安全；它还在创建子进程的线程（而不是进程）结束时就触发。
* 进程已启动、记录尚未写入的几毫秒内控制平面被杀死时，这个进程没有记录。
* 只在 Linux 上（读 ``/proc``）记录；数据库是 ``:memory:`` 时不记录。文件无法读取或内容
  非法时记一条警告并忽略它，下次写入时覆盖。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

#: 记录文件的格式版本。
RECORD_FILE_VERSION = 1


def record_file_path(db_path: str) -> str | None:
    """控制平面数据库 ``db_path`` 对应的实例记录文件（同目录）。

    ``/data/forgemesh.db`` → ``/data/forgemesh.instances.json``；没有扩展名时追加
    ``.instances.json``；``:memory:`` 时为 ``None``（不记录）。
    """
    if db_path == ":memory:":
        return None
    path = Path(db_path)
    if not path.suffix:
        return f"{db_path}.instances.json"
    return str(path.with_suffix(".instances.json"))


def directory_prefix(instance_id: str) -> str:
    """实例私有临时目录的名称前缀；只有这样命名的目录才会按记录删除。"""
    return f"forgemesh-{instance_id}-"


class ProcessRecord(BaseModel):
    """一个实例进程的记录。

    Attributes:
        instance_id: 实例 id（alias ``instanceId``）。
        instance_key: 实例键，即部署运行的幂等键（alias ``instanceKey``）。
        pid: 实例进程的 pid，也是实例的进程组与会话 id。
        start_time: 进程的启动时间：开机以来的时钟滴答数（alias ``startTime``）。
        directories: 实例私有的临时目录。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    instance_id: str = Field(alias="instanceId", min_length=1)
    instance_key: str = Field(alias="instanceKey", min_length=1)
    pid: int = Field(gt=0)
    start_time: int = Field(alias="startTime", ge=0)
    directories: tuple[str, ...] = ()

    def remove_directories(self) -> None:
        """删除实例的私有临时目录（名称不以 :func:`directory_prefix` 开头的路径不动）。"""
        for path in self.directories:
            if os.path.basename(path).startswith(directory_prefix(self.instance_id)):
                shutil.rmtree(path, ignore_errors=True)


class _Content(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    version: Literal[1]
    boot_id: str = Field(alias="bootId", min_length=1)
    pid_namespace: str = Field(alias="pidNamespace", min_length=1)
    instances: tuple[ProcessRecord, ...]


def _host_identity() -> tuple[str, str] | None:
    """本机的 boot id 与本进程所在的 PID 命名空间；读不到（如没有 ``/proc``）时为 ``None``。"""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as file:
            boot_id = file.read().strip()
        return boot_id, os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


class ProcessRecordFile:
    """实例记录文件（见模块说明），只由一个控制平面进程使用。

    创建时读入文件中本次开机、本 PID 命名空间里的记录（:meth:`leftovers`）；之后每次
    :meth:`add` / :meth:`remove` 都整体重写文件（先写临时文件再改名）。

    Args:
        path: 文件路径，见 :func:`record_file_path`。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._identity = _host_identity()
        previous = self._read()
        self._previous = tuple(record.instance_id for record in previous)
        self._records = {record.instance_id: record for record in previous}

    @property
    def path(self) -> str:
        """文件路径。"""
        return self._path

    @property
    def enabled(self) -> bool:
        """本机能否记录实例进程：能从 ``/proc`` 读到 boot id 与 PID 命名空间。"""
        return self._identity is not None

    def leftovers(self) -> list[ProcessRecord]:
        """创建时文件中已有、尚未删去的记录：控制平面上次运行时启动的实例。"""
        return [self._records[key] for key in self._previous if key in self._records]

    def add(self, record: ProcessRecord) -> None:
        """记下 ``record`` 并写入文件；本机不能记录时什么都不做。

        Raises:
            OSError: 无法写入文件；记录未加入。
        """
        records = {**self._records, record.instance_id: record}
        self._write(records)
        self._records = records

    def remove(self, instance_id: str) -> None:
        """删去实例的记录并写入文件；无法写入时只记一条警告。"""
        if self._records.pop(instance_id, None) is None:
            return
        try:
            self._write(self._records)
        except OSError as exc:
            logger.warning("could not update the instance record file %s: %s", self._path, exc)

    def _read(self) -> list[ProcessRecord]:
        try:
            with open(self._path, encoding="utf-8") as file:
                raw = file.read()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("ignoring the instance record file %s: %s", self._path, exc)
            return []
        try:
            content = _Content.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning(
                "ignoring the instance record file %s: it is not a version %d record file: %s",
                self._path,
                RECORD_FILE_VERSION,
                exc,
            )
            return []
        if self._identity != (content.boot_id, content.pid_namespace):
            logger.info(
                "ignoring the instance record file %s: it was written before this host booted "
                "or in another PID namespace, where its processes have ended",
                self._path,
            )
            return []
        return list(content.instances)

    def _write(self, records: dict[str, ProcessRecord]) -> None:
        if self._identity is None:
            return
        boot_id, pid_namespace = self._identity
        content = _Content(
            version=RECORD_FILE_VERSION,
            boot_id=boot_id,
            pid_namespace=pid_namespace,
            instances=tuple(records.values()),
        )
        temporary = f"{self._path}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                file.write(content.model_dump_json(by_alias=True, indent=2) + "\n")
            os.replace(temporary, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


__all__ = [
    "RECORD_FILE_VERSION",
    "ProcessRecord",
    "ProcessRecordFile",
    "directory_prefix",
    "record_file_path",
]
