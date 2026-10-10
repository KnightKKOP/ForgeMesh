"""ForgeMesh 命令行入口骨架（阶段 0，占位）。

提供 ``forgemesh`` 命令与子命令骨架：

* ``forgemesh apply -f <file>``  —— 声明式下发资源（占位，返回未实现）；
* ``forgemesh get <kind>``       —— 查询资源（占位）；
* ``forgemesh describe <kind> <name>`` —— 查看资源详情（占位）；
* ``forgemesh version``          —— 打印版本信息。

阶段 0 不发起任何网络请求，仅校验参数并返回明确的“未实现”提示与退出码：
``0`` 成功、``2`` 参数错误、``3`` 未实现（占位）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

#: CLI 版本。
CLI_VERSION = "0.0.1"

#: 退出码约定。
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3


def _not_implemented(action: str, payload: Optional[dict] = None) -> int:
    """输出统一的“未实现”提示并返回退出码。"""
    message = {
        "error": {
            "code": "NOT_IMPLEMENTED",
            "message": f"阶段 0 占位：`{action}` 尚未实现。",
        }
    }
    if payload:
        message["request"] = payload
    print(json.dumps(message, ensure_ascii=False))
    return EXIT_NOT_IMPLEMENTED


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（骨架）。"""
    parser = argparse.ArgumentParser(
        prog="forgemesh",
        description="ForgeMesh 命令行工具（阶段 0 骨架，占位）。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"forgemesh {CLI_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    apply_parser = subparsers.add_parser("apply", help="声明式下发资源")
    apply_parser.add_argument(
        "-f", "--filename", required=True, help="资源 YAML/JSON 文件路径"
    )
    apply_parser.add_argument(
        "--dry-run", action="store_true", help="仅校验，不提交"
    )

    get_parser = subparsers.add_parser("get", help="查询资源")
    get_parser.add_argument("kind", help="资源类型，如 inferenceservice")
    get_parser.add_argument("name", nargs="?", help="资源名（可选）")
    get_parser.add_argument("-o", "--output", choices=["table", "json", "yaml"],
                            default="table", help="输出格式")

    describe_parser = subparsers.add_parser("describe", help="查看资源详情")
    describe_parser.add_argument("kind", help="资源类型")
    describe_parser.add_argument("name", help="资源名")

    subparsers.add_parser("version", help="打印版本信息")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口。

    Args:
        argv: 参数列表；``None`` 时读取 ``sys.argv[1:]``。

    Returns:
        进程退出码（参见模块级 ``EXIT_*`` 常量）。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    if args.command == "version":
        print(f"forgemesh {CLI_VERSION}")
        return EXIT_OK

    if args.command == "apply":
        return _not_implemented(
            "apply", {"file": args.filename, "dryRun": args.dry_run}
        )

    if args.command == "get":
        return _not_implemented(
            "get",
            {"kind": args.kind, "name": args.name, "output": args.output},
        )

    if args.command == "describe":
        return _not_implemented(
            "describe", {"kind": args.kind, "name": args.name}
        )

    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - 直接执行入口
    sys.exit(main())
