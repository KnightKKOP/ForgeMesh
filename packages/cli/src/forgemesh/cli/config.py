"""CLI 配置：API 地址、API key、租户与命名空间。

* API 地址：``--api-url``，其次 ``FORGEMESH_API_URL``，默认经网关访问
  :data:`DEFAULT_API_URL`；直连控制平面时指向它的地址（默认 ``http://127.0.0.1:8080``）。
  地址是网关或控制平面的基地址（可以带反向代理的路径前缀），不含 ``/api/v1``。
* API key：``--api-key-file`` 指向的文件（只含 key 一行），其次 ``FORGEMESH_API_KEY``；
  都没有时不发送 ``Authorization``。key 只出现在请求头中，任何消息都不包含它。
* 租户：``--tenant`` 作为 ``X-Tenant-Id`` 发送。``dev`` 模式的控制平面以它确定租户；
  带 key 时可以省略，给出就必须与 key 绑定的租户一致。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .errors import UsageError

API_URL_ENV = "FORGEMESH_API_URL"
API_KEY_ENV = "FORGEMESH_API_KEY"
DEFAULT_API_URL = "http://127.0.0.1:8081"

#: RFC 6750 的 ``b64token``：``Authorization: Bearer`` 中允许出现的字符。
_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
#: 租户放在请求头中，只接受可见的 ASCII 字符。
_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]+")


def resolve_api_url(option: str | None, environ: Mapping[str, str]) -> str:
    """API 基地址（不带结尾的 ``/``）。

    Raises:
        UsageError: 不是 http / https 地址，或带有凭证、查询串、片段或 ``/api/v1``。
            消息不包含地址本身（其中可能有凭证）。
    """
    source = "--api-url"
    value = option
    if value is None:
        source = API_URL_ENV
        value = environ.get(API_URL_ENV, "").strip() or DEFAULT_API_URL
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UsageError(f"{source} must be an http or https URL such as {DEFAULT_API_URL}")
    if parts.username is not None or parts.password is not None:
        raise UsageError(f"{source} must not contain credentials; use --api-key-file")
    if parts.query or parts.fragment:
        raise UsageError(f"{source} must not contain a query or a fragment")
    try:
        _ = parts.port  # 端口非法时抛 ValueError
    except ValueError as exc:
        raise UsageError(f"{source} has an invalid port") from exc
    if parts.path.rstrip("/").endswith("/api/v1"):
        raise UsageError(f"{source} is the base URL of the API; remove the trailing /api/v1")
    return value.rstrip("/")


def resolve_api_key(key_file: str | None, environ: Mapping[str, str]) -> str | None:
    """API key：``key_file`` 优先，其次 ``FORGEMESH_API_KEY``（空白值视同未设置）。

    Raises:
        UsageError: 文件不可读，或内容不是一个 key。消息不包含文件内容。
    """
    if key_file is not None:
        source = f"the API key file {key_file}"
        try:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise UsageError(f"{source} is not UTF-8 text") from exc
        except OSError as exc:
            raise UsageError(f"cannot read {source}: {exc.strerror or exc}") from exc
        if not key:
            raise UsageError(f"{source} is empty")
    else:
        source = API_KEY_ENV
        key = environ.get(API_KEY_ENV, "").strip()
        if not key:
            return None
    if not _TOKEN.fullmatch(key):
        raise UsageError(
            f"{source} does not hold a single API key: expected one line with the key "
            "printed by `python -m forgemesh.controlplane.keygen`"
        )
    return key


def check_tenant(value: str, source: str) -> str:
    """校验作为 ``X-Tenant-Id`` 发送的租户。

    Raises:
        UsageError: 为空，或含有空白、控制字符或非 ASCII 字符。
    """
    if not _VISIBLE_ASCII.fullmatch(value):
        raise UsageError(f"{source} must be a non-empty string of visible ASCII characters")
    return value


def check_namespace(value: str, source: str) -> str:
    """校验命名空间。

    API 把空字符串当作一个命名空间，与不带命名空间（省略）不是同一个；空字符串几乎总是
    笔误，因此拒绝。

    Raises:
        UsageError: 为空字符串。
    """
    if not value:
        raise UsageError(f"{source} must not be empty; omit it for resources without a namespace")
    return value
