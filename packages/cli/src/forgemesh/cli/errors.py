"""CLI 的错误与退出码。

命令以异常报告失败：:class:`CliError` 的 ``message`` 写到标准错误，``exit_code`` 是进程的
退出码。退出码按失败原因区分，脚本可以据此决定是否重试（见 :class:`ExitCode`）。
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """进程退出码，``forgemesh --help`` 列出同一张表。"""

    OK = 0
    ERROR = 1
    """其他错误，如响应不是来自 ForgeMesh API（地址配置错误）或无法解析。"""
    USAGE = 2
    """参数、配置或资源文档不合法，没有发出请求（与 argparse 的用法错误相同）。"""
    NOT_FOUND = 3
    """API 报告资源不存在（``404`` 错误信封，错误码 ``NOT_FOUND``）。"""
    CONFLICT = 4
    REJECTED = 5
    """API 拒绝了请求：``400``、``405``、``413``、``415`` 等其他 4xx。"""
    AUTH = 6
    """``401`` / ``403``：key 缺失或无效，或者无权执行请求。"""
    RATE_LIMITED = 7
    SERVER_ERROR = 8
    """``500`` 等其他 5xx。"""
    UNAVAILABLE = 9
    """``502`` / ``503`` / ``504``：网关连不上控制平面，或控制平面的组件尚未就绪。"""
    CONNECTION_FAILED = 10
    """没有收到 HTTP 响应：连接被拒绝、主机名无法解析、TLS 失败或超时。"""
    WAIT_TIMEOUT = 11
    WAIT_FAILED = 12
    """等待的条件不会满足：服务正在删除，或在当前的 ``spec`` 下进入 ``Failed``（``spec`` 变化之前
    控制器不会再部署它）。"""
    INTERRUPTED = 130
    """被 Ctrl-C 中断（128 + SIGINT，与 shell 的约定相同）。"""


def exit_code_for_status(status: int) -> ExitCode:
    """错误状态码对应的退出码。"""
    if status in (401, 403):
        return ExitCode.AUTH
    if status == 404:
        return ExitCode.NOT_FOUND
    if status == 409:
        return ExitCode.CONFLICT
    if status == 429:
        return ExitCode.RATE_LIMITED
    if status in (502, 503, 504):
        return ExitCode.UNAVAILABLE
    if 400 <= status < 500:
        return ExitCode.REJECTED
    if status >= 500:
        return ExitCode.SERVER_ERROR
    return ExitCode.ERROR


class CliError(Exception):
    """命令失败：``message`` 写到标准错误，进程以 ``exit_code`` 退出。"""

    def __init__(self, message: str, exit_code: ExitCode = ExitCode.ERROR) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class UsageError(CliError):
    """参数、配置或资源文档不合法。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, ExitCode.USAGE)


class ConnectionFailure(CliError):
    """没有收到 HTTP 响应。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, ExitCode.CONNECTION_FAILED)


class ApiError(CliError):
    """API 以错误状态码响应。

    Attributes:
        status: HTTP 状态码。
        code: 错误信封的 ``error.code``；响应不是错误信封（如反向代理的纯文本 ``502``）时为
            ``None``。
        reason: 错误信封的 ``details.reason``（如 ``409`` 的 ``ResourceVersionMismatch``）。
        retry_after: ``Retry-After`` 响应头给出的秒数。
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        reason: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        exit_code = exit_code_for_status(status)
        if exit_code is ExitCode.NOT_FOUND and code != "NOT_FOUND":
            # 不是 API 的 404（如网关或控制平面没有路由的路径）：地址错了，不能让脚本以为资源不存在
            exit_code = ExitCode.ERROR
        super().__init__(message, exit_code)
        self.status = status
        self.code = code
        self.reason = reason
        self.retry_after = retry_after

    @property
    def not_found(self) -> bool:
        """资源不存在：错误码为 ``NOT_FOUND`` 的 ``404``，而不是网关与控制平面对没有路由的路径
        返回的 ``ROUTE_NOT_FOUND`` 或不提供 API 的地址返回的 ``404``。"""
        return self.status == 404 and self.code == "NOT_FOUND"

    @property
    def transient(self) -> bool:
        """稍后重试可能成功：限流，或网关、控制平面暂时不可用。"""
        return self.exit_code in (ExitCode.RATE_LIMITED, ExitCode.UNAVAILABLE)
