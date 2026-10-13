"""vLLM OpenAI 兼容服务的替身，供 vLLM 进程启动器的测试代替 ``python -m vllm...`` 运行。

接受启动器追加的 ``--model=… --host … --port … --tensor-parallel-size …``，每次启动向
``--report`` 文件追加一行 JSON（参数、进程与会话 id、相关环境变量、派生的子进程），然后按
``--mode`` 行事：``serve``（``--delay`` 秒后监听，``GET /health`` 返回 200）、``exit``（以
退出码 3 结束）、``hang``（从不监听）。``--ignore-sigterm`` 忽略 ``SIGTERM``；
``--spawn-child`` 派生一个忽略 ``SIGTERM`` 的子进程（同一进程组）。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_CHILD = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--mode", choices=["serve", "exit", "hang"], default="serve")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--ignore-sigterm", action="store_true")
    parser.add_argument("--spawn-child", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tensor-parallel-size", type=int, required=True)
    args = parser.parse_args()
    if args.ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", _CHILD]) if args.spawn_child else None
    report = {
        "argv": sys.argv[1:],
        "model": args.model,
        "host": args.host,
        "port": args.port,
        "tensor_parallel_size": args.tensor_parallel_size,
        "pid": os.getpid(),
        "sid": os.getsid(0),
        "child_pid": child.pid if child else None,
        "env": {
            name: os.environ.get(name)
            for name in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "FORGEMESH_STANDIN_SECRET")
        },
    }
    with open(args.report, "a", encoding="utf-8") as file:
        file.write(json.dumps(report) + "\n")
    if args.mode == "exit":
        sys.exit(3)
    if args.mode == "hang":
        while True:
            time.sleep(60)
    time.sleep(args.delay)
    HTTPServer((args.host, args.port), _Health).serve_forever()


if __name__ == "__main__":
    main()
