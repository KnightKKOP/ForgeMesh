"""TGI 进程启动器：每个实例是控制平面所在主机上的一个 TGI（text-generation-inference）服务。

:class:`TGIProcessLauncher` 以 ``<command> --model-id <spec.model.reference> --hostname
<bind_host> --port <port> --num-shard <设备数> --master-port <port> --shard-uds-path
<私有目录>/shard`` 启动实例。``command`` 默认是 ``PATH`` 中的 ``text-generation-launcher``，
可换成其他位置的 TGI（或测试用的替身）。只服务 ``spec.runtime.provider`` 为 ``tgi`` 的
资源，实例提供 TGI 的生成接口（协议 ``tgi-generate``，网关转发到 ``/generate_stream``）。
环境变量、设备、就绪检查、停止与控制平面重启见 :mod:`.process`。

TGI 的默认值会让同一主机上的实例相互冲突：router 与 shard 之间的 gRPC 套接字默认是
``/tmp/text-generation-server-<rank>``，shard 之间 torch distributed 的端口默认是
``29500``。启动器给每个实例另一个空闲端口作 ``--master-port``，并把套接字放在实例私有的
临时目录中（权限 ``0700``，实例停止之后删除）。``text-generation-launcher`` 把 router 与
每个 shard 各放在自己的进程组中，``stop`` 结束实例会话中的全部进程组。
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from forgemesh.resource.inference import EndpointProtocol

from .launcher import LaunchRequest
from .process import LaunchResources, ProcessLauncher

#: 默认命令：``PATH`` 中的 TGI 启动程序。
DEFAULT_COMMAND: tuple[str, ...] = ("text-generation-launcher",)
#: 服务的运行时（``spec.runtime.provider``）。
PROVIDER = "tgi"


class TGIProcessLauncher(ProcessLauncher):
    """把每个实例作为本机上的 TGI 子进程运行的启动器。

    参数见 :class:`~.process.ProcessLauncher`；``command`` 默认 :data:`DEFAULT_COMMAND`。
    """

    runtime_name = "TGI"
    launcher_name = "tgi-process"
    provider = PROVIDER
    protocol = EndpointProtocol.TGI_GENERATE
    default_command = DEFAULT_COMMAND

    def _arguments(
        self, request: LaunchRequest, devices: Sequence[str], port: int, resources: LaunchResources
    ) -> list[str]:
        return [
            "--model-id",
            request.spec.model.reference,
            "--hostname",
            self.bind_host,
            "--port",
            str(port),
            "--num-shard",
            str(max(len(devices), 1)),
            "--master-port",
            str(resources.port()),
            "--shard-uds-path",
            os.path.join(resources.directory(), "shard"),
        ]


__all__ = ["DEFAULT_COMMAND", "PROVIDER", "TGIProcessLauncher"]
