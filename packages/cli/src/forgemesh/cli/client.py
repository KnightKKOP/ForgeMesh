"""ForgeMesh REST API 的 HTTP 客户端，只用标准库（:mod:`urllib.request`）。

网关与控制平面提供同一套 ``/api/v1`` API。一次命令行调用的全部请求带同一个
``X-Correlation-Id``，错误消息中显示它，便于在网关与控制平面的日志中找到这次调用。
错误响应是控制平面与网关的错误信封，或网关自己的纯文本响应（限流 ``429``、连不上控制
平面 ``502``）；两者都转换为 :class:`~.errors.ApiError`。客户端不跟随重定向，
``Authorization`` 只发给配置的地址。
"""

from __future__ import annotations

import copy
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from email.message import Message
from http import HTTPStatus
from typing import IO, Any
from urllib.parse import quote, urlencode

from .config import API_KEY_ENV, API_URL_ENV
from .errors import ApiError, CliError, ConnectionFailure
from .output import printable

SERVICES_PATH = "/api/v1/inference-services"
CORRELATION_HEADER = "X-Correlation-Id"
TENANT_HEADER = "X-Tenant-Id"
USER_AGENT = "forgemesh-cli"
#: 单个请求的超时（秒）。网关等待控制平面的响应头最多 30 秒，超过时返回 ``502``。
REQUEST_TIMEOUT_SECONDS = 60.0
#: 错误消息引用非错误信封的响应正文时最多保留的字符数。
_QUOTE_LIMIT = 200


@dataclass(frozen=True)
class Response:
    """成功（2xx）的响应。

    Attributes:
        status: HTTP 状态码。
        body: 解析后的 JSON 正文；没有正文（如 ``204``）时为 ``None``。
    """

    status: int
    body: Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 作为错误返回，不跟随：请求头中的凭证不会被发到其他地址。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


class ApiClient:
    """``/api/v1`` 的客户端。

    Args:
        base_url: 网关或控制平面的基地址（见 :func:`~.config.resolve_api_url`）。
        api_key: ``Authorization: Bearer`` 的 key；为 ``None`` 时不发送该请求头。
        tenant: ``X-Tenant-Id`` 请求头；为 ``None`` 时不发送。
        correlation_id: 每个请求的 ``X-Correlation-Id``。
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None,
        tenant: str | None,
        correlation_id: str,
    ) -> None:
        self.base_url = base_url
        self.tenant = tenant
        self.correlation_id = correlation_id
        self._api_key = api_key
        self._opener = urllib.request.build_opener(_NoRedirect)

    def with_tenant(self, tenant: str | None) -> ApiClient:
        """配置相同、``X-Tenant-Id`` 为 ``tenant`` 的客户端。"""
        client = copy.copy(self)
        client.tenant = tenant
        return client

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int] | None = None,
        body: Any = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Response:
        """发送一个请求，返回成功的响应。

        Raises:
            ApiError: 响应的状态码不是 2xx（包括没有跟随的重定向）。
            ConnectionFailure: 没有收到 HTTP 响应。
            CliError: 成功响应的正文不是 JSON。
        """
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            CORRELATION_HEADER: self.correlation_id,
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self.tenant is not None:
            headers[TENANT_HEADER] = self.tenant
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except (OSError, http.client.HTTPException):
                raw = b""
            finally:
                exc.close()
            raise self._error(exc.code, exc.headers, raw) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            cause = printable(reason) or type(reason).__name__
            raise ConnectionFailure(
                f"cannot reach the API at {self.base_url}: {cause} (check --api-url or "
                f"{API_URL_ENV}, and that the gateway or control plane is running)"
            ) from exc
        if not raw:
            return Response(status, None)
        try:
            return Response(status, json.loads(raw))
        except ValueError as exc:
            raise CliError(
                f"{method} {path} at {self.base_url} answered HTTP {status} with a body that "
                "is not JSON; check --api-url"
            ) from exc

    def get_service(
        self, name: str, namespace: str | None, *, timeout: float = REQUEST_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """读取一个推理服务。"""
        response = self.request(
            "GET", _service_path(name), query=_namespace_query(namespace), timeout=timeout
        )
        return self._resource(response)

    def create_service(self, document: dict[str, Any]) -> dict[str, Any]:
        """创建推理服务（``201``）。"""
        return self._resource(self.request("POST", SERVICES_PATH, body=document))

    def replace_service(
        self, name: str, namespace: str | None, document: dict[str, Any]
    ) -> dict[str, Any]:
        """整体替换推理服务（``PUT``）。"""
        response = self.request(
            "PUT", _service_path(name), query=_namespace_query(namespace), body=document
        )
        return self._resource(response)

    def delete_service(self, name: str, namespace: str | None) -> int:
        """受控删除，返回状态码：``204`` 已删除，``202`` 进入 ``Terminating``。"""
        return self.request("DELETE", _service_path(name), query=_namespace_query(namespace)).status

    def list_services(self, namespace: str | None, *, page_size: int) -> Iterator[dict[str, Any]]:
        """逐页列出推理服务：以相同的过滤条件把 ``nextPageToken`` 作为 ``pageToken`` 传回，
        直到响应不再带它。

        Args:
            namespace: 只列出这个命名空间；``None`` 时列出全部。
            page_size: 每页条数（``limit``，1–1000）。
        """
        query: dict[str, str | int] = {"limit": page_size}
        if namespace is not None:
            query["namespace"] = namespace
        token: str | None = None
        while True:
            page = self.request(
                "GET",
                SERVICES_PATH,
                query=query if token is None else {**query, "pageToken": token},
            ).body
            items = page.get("items") if isinstance(page, dict) else None
            next_token = page.get("nextPageToken") if isinstance(page, dict) else None
            if (
                not isinstance(items, list)
                or not all(isinstance(item, dict) for item in items)
                or (next_token is not None and (not isinstance(next_token, str) or not next_token))
            ):
                raise self._unexpected("list")
            yield from items
            if next_token is None:
                return
            if next_token == token:
                raise CliError(
                    "the API returned the same nextPageToken twice; the list is incomplete"
                )
            token = next_token

    def _resource(self, response: Response) -> dict[str, Any]:
        body = response.body
        if not isinstance(body, dict) or not isinstance(body.get("metadata"), dict):
            raise self._unexpected("resource")
        return body

    def _unexpected(self, what: str) -> CliError:
        return CliError(
            f"the API at {self.base_url} returned an unexpected {what} response; check --api-url"
        )

    def _error(self, status: int, headers: Message, raw: bytes) -> ApiError:
        retry_after = _retry_after(headers.get("Retry-After"))
        error = _envelope(raw)
        code = reason = None
        if error is not None:
            code = error["code"]
            details = error.get("details")
            if isinstance(details, dict) and isinstance(details.get("reason"), str):
                reason = details["reason"]
            correlation_id = error.get("correlationId")
            if not isinstance(correlation_id, str) or not correlation_id:
                correlation_id = self.correlation_id
            summary = f"HTTP {status} {code}" + (f", reason {reason}" if reason else "")
            lines = [
                f"{printable(error['message'])} ({printable(summary)}, "
                f"correlation id {printable(correlation_id)})",
                *_validation_errors(details),
            ]
        else:
            lines = [
                f"{_plain_message(status, headers, raw)} (correlation id {self.correlation_id})"
            ]
        if retry_after is not None:
            lines[0] += f"; retry after {retry_after} s"
        if status == 401 and self._api_key is None:
            lines.append(
                f"no API key is configured: set {API_KEY_ENV} or pass --api-key-file "
                "(a control plane in dev mode needs --tenant instead)"
            )
        return ApiError(status, "\n".join(lines), code=code, reason=reason, retry_after=retry_after)


def _service_path(name: str) -> str:
    return f"{SERVICES_PATH}/{quote(name, safe='')}"


def _namespace_query(namespace: str | None) -> dict[str, str] | None:
    return None if namespace is None else {"namespace": namespace}


def _envelope(raw: bytes) -> dict[str, Any] | None:
    """错误信封中的 ``error`` 对象；正文不是错误信封时返回 ``None``。"""
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if (
        isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and isinstance(error.get("message"), str)
    ):
        return error
    return None


def _validation_errors(details: Any) -> list[str]:
    """``details.errors``（请求校验失败）逐条列出，如 ``  spec.resources: Field required``。"""
    errors = details.get("errors") if isinstance(details, dict) else None
    lines = []
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        loc = error.get("loc")
        where = ".".join(str(part) for part in loc) if isinstance(loc, list) and loc else "-"
        lines.append(f"  {printable(where)}: {printable(error.get('msg', ''))}")
    return lines


def _plain_message(status: int, headers: Message, raw: bytes) -> str:
    """不是错误信封的错误响应（网关的纯文本响应，或不提供 API 的地址）的说明。"""
    status_line = f"HTTP {status} {_phrase(status)}".rstrip()
    if status == 429:
        return f"rate limited by the gateway ({status_line})"
    if status == 502:
        return f"the gateway could not reach the control plane ({status_line})"
    if 300 <= status < 400:
        location = headers.get("Location")
        target = f" to {printable(location)}" if location else ""
        return (
            f"{status_line}: redirected{target}; the CLI does not follow redirects, set "
            "--api-url to the address that serves the API"
        )
    text = _quote(raw)
    message = f"{status_line}: {text}" if text else status_line
    if 400 <= status < 500:
        message += "; the response is not from the ForgeMesh API, check --api-url"
    return message


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _quote(raw: bytes) -> str:
    lines = raw.decode("utf-8", errors="replace").strip().splitlines()
    text = printable(lines[0].strip()) if lines else ""
    return text if len(text) <= _QUOTE_LIMIT else text[:_QUOTE_LIMIT] + "..."


def _retry_after(value: str | None) -> int | None:
    """``Retry-After`` 的秒数；不是秒数（如 HTTP 日期）时返回 ``None``。"""
    text = (value or "").strip()
    return int(text) if text.isascii() and text.isdigit() else None
