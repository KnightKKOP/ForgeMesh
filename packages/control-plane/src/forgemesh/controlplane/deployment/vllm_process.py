"""vLLM 进程启动器：每个实例是控制平面所在主机上的一个 vLLM OpenAI 兼容服务进程。

:class:`VLLMProcessLauncher` 以 ``<command> --model=<spec.model.reference> --host <bind_host>
--port <port> --tensor-parallel-size <设备数>`` 启动实例。``command`` 默认是
``<控制平面的解释器> -m vllm.entrypoints.openai.api_server``，可换成其他环境中的 vLLM（或
测试用的替身）。只服务 ``spec.runtime.provider`` 为 ``vllm`` 的资源，实例提供 OpenAI 兼容
接口（协议 ``openai-chat``）。环境变量、设备、就绪检查、停止与控制平面重启见
:mod:`.process`。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from forgemesh.resource.inference import EndpointProtocol

from .launcher import LaunchRequest
from .process import (
    DEFAULT_BIND_HOST,
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_STOP_GRACE_SECONDS,
    KILL_WAIT_SECONDS,
    ProcessLauncher,
    cuda_device,
)

#: vLLM 的 OpenAI 兼容服务入口模块。
VLLM_MODULE = "vllm.entrypoints.openai.api_server"
#: 默认命令：以控制平面的解释器运行 vLLM。
DEFAULT_COMMAND: tuple[str, ...] = (sys.executable, "-m", VLLM_MODULE)
#: 服务的运行时（``spec.runtime.provider``）。
PROVIDER = "vllm"


class VLLMProcessLauncher(ProcessLauncher):
    """把每个实例作为本机上的 vLLM 子进程运行的启动器。

    参数见 :class:`~.process.ProcessLauncher`；``command`` 默认 :data:`DEFAULT_COMMAND`。
    """

    runtime_name = "vLLM"
    launcher_name = "vllm-process"
    provider = PROVIDER
    protocol = EndpointProtocol.OPENAI_CHAT
    default_command = DEFAULT_COMMAND

    def _arguments(self, request: LaunchRequest, devices: Sequence[str], port: int) -> list[str]:
        return [
            f"--model={request.spec.model.reference}",
            "--host",
            self.bind_host,
            "--port",
            str(port),
            "--tensor-parallel-size",
            str(max(len(devices), 1)),
        ]


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
