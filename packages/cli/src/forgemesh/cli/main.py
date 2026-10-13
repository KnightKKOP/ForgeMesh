"""ForgeMesh 命令行工具：经 REST API 管理推理服务（``InferenceService``）。

命令::

  forgemesh apply -f FILE [-f FILE ...]       资源不存在就创建，存在就更新
  forgemesh get inferenceservice NAME [-o yaml|json]
  forgemesh describe inferenceservice NAME
  forgemesh list inferenceservices [-o table|yaml|json] [--page-size N]
  forgemesh delete inferenceservice NAME      受控删除
  forgemesh wait inferenceservice NAME [--for ready|delete] [--timeout SECONDS]

公共选项 ``--api-url``、``--api-key-file``、``--tenant`` 与 ``-n`` / ``--namespace`` 可以写在子命令
之前或之后（之后的优先）。默认经网关访问 API；地址、key 与租户的来源见 :mod:`.config`，
退出码见 :class:`~.errors.ExitCode`。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import uuid
from collections.abc import Callable

from .client import ApiClient
from .commands import cmd_apply, cmd_delete, cmd_describe, cmd_get, cmd_list, cmd_wait
from .config import (
    API_KEY_ENV,
    API_URL_ENV,
    DEFAULT_API_URL,
    check_namespace,
    check_tenant,
    resolve_api_key,
    resolve_api_url,
)
from .documents import KIND
from .errors import CliError, ExitCode

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
DEFAULT_WAIT_TIMEOUT_SECONDS = 300.0

_KIND_NAMES = frozenset({"inferenceservice", "inferenceservices"})

_EPILOG = f"""\
environment variables:
  {API_URL_ENV}  base URL of the API if --api-url is not given (default
                     {DEFAULT_API_URL}, the gateway; the control plane
                     itself listens on http://127.0.0.1:8080 by default)
  {API_KEY_ENV}  API key if --api-key-file is not given

exit codes:
  0    success
  1    other errors, e.g. the URL does not serve the ForgeMesh API
  2    invalid arguments, configuration or resource documents (nothing sent)
  3    not found (HTTP 404); wait --for delete exits 0 instead
  4    conflict (HTTP 409), e.g. the service is being deleted
  5    request rejected (HTTP 400, 405, 413, 415 and other 4xx)
  6    authentication or authorization failed (HTTP 401, 403)
  7    rate limited (HTTP 429)
  8    server error (HTTP 500 and other 5xx)
  9    API unavailable (HTTP 502, 503, 504)
  10   no HTTP response: connection refused, unknown host, TLS error, timeout
  11   wait timed out
  12   wait failed: the service became Failed or is being deleted
  130  interrupted
"""

Handler = Callable[[argparse.Namespace, ApiClient], int]


def build_parser() -> argparse.ArgumentParser:
    """命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="forgemesh",
        description="Manage ForgeMesh inference services through the REST API.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_options(parser, None)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    apply = _add_command(
        commands, "apply", cmd_apply, "create the services that do not exist, update the others"
    )
    apply.add_argument(
        "-f",
        "--file",
        dest="files",
        action="append",
        required=True,
        metavar="FILE",
        help="YAML (several documents allowed) or JSON file, - for standard input; repeatable",
    )

    get = _add_command(commands, "get", cmd_get, "print a service")
    _add_target(get)
    get.add_argument(
        "-o", "--output", choices=("yaml", "json"), default="yaml", help="default: yaml"
    )

    describe = _add_command(
        commands, "describe", cmd_describe, "show a service with its conditions and endpoints"
    )
    _add_target(describe)

    listing = _add_command(commands, "list", cmd_list, "list services, reading every page")
    listing.add_argument("kind", type=_kind, metavar="KIND", help="inferenceservices")
    listing.add_argument(
        "-o", "--output", choices=("table", "yaml", "json"), default="table", help="default: table"
    )
    listing.add_argument(
        "--page-size",
        type=_page_size,
        default=DEFAULT_PAGE_SIZE,
        metavar="N",
        help=f"services per request, 1 to {MAX_PAGE_SIZE} (default: {DEFAULT_PAGE_SIZE})",
    )

    delete = _add_command(
        commands, "delete", cmd_delete, "delete a service; its instance is stopped first"
    )
    _add_target(delete)

    wait = _add_command(commands, "wait", cmd_wait, "wait until a service is Ready or deleted")
    _add_target(wait)
    wait.add_argument(
        "--for",
        dest="condition",
        choices=("ready", "delete"),
        default="ready",
        help="default: ready; fails at once if the service becomes Failed or is being deleted",
    )
    wait.add_argument(
        "--timeout",
        type=_seconds,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"give up after this many seconds (default: {DEFAULT_WAIT_TIMEOUT_SECONDS:g})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口，返回退出码（见 :class:`~.errors.ExitCode`）；参数非法时 argparse 以 2 退出。"""
    args = build_parser().parse_args(argv)
    try:
        if args.tenant is not None:
            check_tenant(args.tenant, "--tenant")
        if args.namespace is not None:
            check_namespace(args.namespace, "--namespace")
        client = ApiClient(
            resolve_api_url(args.api_url, os.environ),
            api_key=resolve_api_key(args.api_key_file, os.environ),
            tenant=args.tenant,
            correlation_id=uuid.uuid4().hex,
        )
        handler: Handler = args.handler
        return handler(args, client)
    except CliError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED


def _add_common_options(parser: argparse.ArgumentParser, default: str | None) -> None:
    """公共选项。子命令以 ``SUPPRESS`` 为默认值：子命令的默认值会覆盖写在子命令之前的选项。"""
    parser.add_argument(
        "--api-url",
        metavar="URL",
        default=default,
        help=f"base URL of the gateway or the control plane, without /api/v1 "
        f"(default: ${API_URL_ENV}, else {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--api-key-file",
        metavar="PATH",
        default=default,
        help=f"file that holds the API key (default: ${API_KEY_ENV}; "
        "without a key no Authorization header is sent)",
    )
    parser.add_argument(
        "--tenant",
        default=default,
        help="tenant sent as X-Tenant-Id: required by a control plane in dev mode; "
        "with an API key it must be the tenant of the key",
    )
    parser.add_argument(
        "-n",
        "--namespace",
        default=default,
        help="namespace of the service; without it the command addresses a service "
        "without a namespace (list: every namespace)",
    )


def _add_command(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    handler: Handler,
    summary: str,
) -> argparse.ArgumentParser:
    parser = commands.add_parser(name, help=summary, description=summary[:1].upper() + summary[1:])
    _add_common_options(parser, argparse.SUPPRESS)
    parser.set_defaults(handler=handler)
    return parser


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("kind", type=_kind, metavar="KIND", help="inferenceservice")
    parser.add_argument("name", metavar="NAME")


def _kind(value: str) -> str:
    if value.lower() not in _KIND_NAMES:
        raise argparse.ArgumentTypeError(
            f"unsupported kind {value!r}; the CLI manages {KIND} (inferenceservice)"
        )
    return KIND


def _page_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        size = 0
    if not 1 <= size <= MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {MAX_PAGE_SIZE}")
    return size


def _seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not (math.isfinite(seconds) and seconds >= 0):
        raise argparse.ArgumentTypeError("must be a number of seconds, 0 or more")
    return seconds


if __name__ == "__main__":  # pragma: no cover - 直接执行入口
    sys.exit(main())
