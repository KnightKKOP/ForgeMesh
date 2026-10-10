"""ForgeMesh CLI — 声明式资源管理工具。

命令::

  forgemesh apply -f <file.yaml>    创建/更新资源
  forgemesh get <kind> <name>       查询资源
  forgemesh describe <kind> <name>  查询资源详情
  forgemesh list <kind>             列出资源
  forgemesh delete <kind> <name>    删除资源

全局选项::

  --tenant <id>       租户 ID（默认 ``default``）
  --namespace <ns>    命名空间（可选）
  --api-url <url>     API 地址（默认 ``http://localhost:8080``，P1 直连 Registry）

P1 简化：CLI 直接调用 :class:`SQLiteRegistry`（内存库），不走 HTTP。
后续阶段将切换为 HTTP 客户端模式。

兼容 Python 3.9+。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .commands import cmd_apply, cmd_delete, cmd_describe, cmd_get, cmd_list


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。

    Args:
        argv: 参数列表；``None`` 时读取 ``sys.argv[1:]``。

    Returns:
        进程退出码（``0`` 成功，``1`` 业务错误）。
    """
    parser = argparse.ArgumentParser(
        prog="forgemesh", description="ForgeMesh CLI — 声明式资源管理工具"
    )
    parser.add_argument("--tenant", default="default", help="租户 ID")
    parser.add_argument("--namespace", default=None, help="命名空间")
    parser.add_argument(
        "--api-url", default="http://localhost:8080", help="API 地址"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # apply
    p_apply = subparsers.add_parser("apply", help="创建/更新资源")
    p_apply.add_argument("-f", "--file", required=True, help="YAML 文件路径")

    # get
    p_get = subparsers.add_parser("get", help="查询资源")
    p_get.add_argument("kind", help="资源类型")
    p_get.add_argument("name", help="资源名称")

    # describe
    p_describe = subparsers.add_parser("describe", help="查询资源详情")
    p_describe.add_argument("kind", help="资源类型")
    p_describe.add_argument("name", help="资源名称")

    # list
    p_list = subparsers.add_parser("list", help="列出资源")
    p_list.add_argument("kind", help="资源类型")

    # delete
    p_delete = subparsers.add_parser("delete", help="删除资源")
    p_delete.add_argument("kind", help="资源类型")
    p_delete.add_argument("name", help="资源名称")

    args = parser.parse_args(argv)

    commands = {
        "apply": cmd_apply,
        "get": cmd_get,
        "describe": cmd_describe,
        "list": cmd_list,
        "delete": cmd_delete,
    }

    handler = commands[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":  # pragma: no cover - 直接执行入口
    sys.exit(main())
