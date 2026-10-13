"""被 ``SIGKILL`` 的控制平面的替身：用进程启动器启动一个实例、等它就绪，然后一直等待。

参数依次是启动器的类名（:mod:`forgemesh.controlplane.deployment` 中的
``VLLMProcessLauncher`` / ``TGIProcessLauncher``）、实例记录文件、启动请求（JSON）、命令
（JSON 数组）与就绪标记文件。实例就绪之后创建标记文件，然后睡眠，直到被测试杀死。标记
不写到标准输出：实例继承了标准输出，管道要到实例结束才会关闭。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from forgemesh.controlplane import deployment


async def main() -> None:
    name, record_file, request, command, ready = sys.argv[1:]
    launcher = getattr(deployment, name)(
        "node-a",
        command=json.loads(command),
        record_file=record_file,
        ready_timeout=10.0,
        poll_interval=0.02,
    )
    instance = await launcher.launch(deployment.LaunchRequest.model_validate_json(request))
    await launcher.verify(instance)
    Path(ready).touch()
    await asyncio.sleep(600)


if __name__ == "__main__":
    asyncio.run(main())
