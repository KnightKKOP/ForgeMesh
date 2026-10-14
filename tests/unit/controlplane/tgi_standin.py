"""TGI 的替身，供 TGI 进程启动器的测试代替 ``text-generation-launcher`` 运行。

接受启动器追加的 ``--model-id … --hostname … --port … --num-shard … --master-port …
--shard-uds-path …``，像 shard 那样在 ``<shard-uds-path>-0`` 上监听 Unix 套接字，每次启动向
``--report`` 文件追加一行 JSON（参数、进程与会话 id、套接字目录的权限、相关环境变量、派生
的子进程），然后按 ``--mode`` 行事：``serve``（``GET /health`` 返回 200）或 ``exit``（以
退出码 3 结束）。``--spawn-child`` 像 TGI 的 router 与 shard 那样派生一个在自己的进程组中、
忽略 ``SIGTERM`` 的子进程。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
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
    parser.add_argument("--mode", choices=["serve", "exit"], default="serve")
    parser.add_argument("--spawn-child", action="store_true")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--num-shard", type=int, required=True)
    parser.add_argument("--master-port", type=int, required=True)
    parser.add_argument("--shard-uds-path", required=True)
    args = parser.parse_args()
    shard = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    shard.bind(f"{args.shard_uds_path}-0")
    shard.listen()
    child = (
        subprocess.Popen([sys.executable, "-c", _CHILD], process_group=0)
        if args.spawn_child
        else None
    )
    directory = os.path.dirname(args.shard_uds_path)
    report = {
        "argv": sys.argv[1:],
        "port": args.port,
        "master_port": args.master_port,
        "shard_uds_path": args.shard_uds_path,
        "shard_directory_mode": stat.S_IMODE(os.stat(directory).st_mode),
        "pid": os.getpid(),
        "sid": os.getsid(0),
        "child_pid": child.pid if child else None,
        "child_pgid": os.getpgid(child.pid) if child else None,
        "env": {
            name: os.environ.get(name)
            for name in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "FORGEMESH_STANDIN_SECRET")
        },
    }
    with open(args.report, "a", encoding="utf-8") as file:
        file.write(json.dumps(report) + "\n")
    if args.mode == "exit":
        sys.exit(3)
    HTTPServer((args.hostname, args.port), _Health).serve_forever()


if __name__ == "__main__":
    main()
