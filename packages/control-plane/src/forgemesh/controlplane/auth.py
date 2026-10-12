"""控制平面认证：把请求凭证解析为已验证的调用主体（:class:`Principal`）。

租户身份必须来自经过验证的凭证，而不是客户端可以随意填写的请求头；控制平面
自己完成校验，不依赖“请求一定经过网关”这一假设（见 ADR-014）。

认证模式由环境变量 ``FORGEMESH_AUTH_MODE`` 选择：

* ``apikey``（默认）：``Authorization: Bearer <key>``。key 与租户、主体、scope 的
  绑定关系来自 ``FORGEMESH_API_KEYS_FILE`` 指向的 JSON 文件，文件中只保存 key 的
  SHA-256 摘要。``X-Tenant-Id`` 请求头可以省略；如果携带，必须与凭证绑定的租户
  一致，否则拒绝（403），避免请求被静默地写进另一个租户。
* ``dev``：直接信任 ``X-Tenant-Id`` 请求头，仅供本地开发；
  :func:`forgemesh.controlplane.app.main` 只允许在回环地址上以该模式启动。

key 文件格式（与网关的 ``GATEWAY_API_KEYS_FILE`` 共用）::

    {"keys": [{"id": "acme-ci", "sha256": "<64 位十六进制>", "tenantId": "acme",
               "subject": "ci-bot", "scopes": ["read", "write"]}]}

生成新 key：``python -m forgemesh.controlplane.auth keygen --tenant acme --subject ci-bot``。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

AUTH_MODE_ENV = "FORGEMESH_AUTH_MODE"
API_KEYS_FILE_ENV = "FORGEMESH_API_KEYS_FILE"
AUTH_MODE_APIKEY = "apikey"
AUTH_MODE_DEV = "dev"
DEFAULT_AUTH_MODE = AUTH_MODE_APIKEY

#: 承载租户声明的请求头。
TENANT_HEADER = "X-Tenant-Id"

SCOPE_READ = "read"
SCOPE_WRITE = "write"
#: 通配 scope，持有者视为拥有全部 scope。
SCOPE_ALL = "*"
KNOWN_SCOPES = frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ALL})

#: 生成的 key 的前缀，便于在日志和密钥扫描中识别。
API_KEY_PREFIX = "fmk_"


class AuthenticationError(Exception):
    """凭证缺失或无效（HTTP 401）。"""


class PermissionDeniedError(Exception):
    """凭证有效，但无权执行该请求（HTTP 403）。"""


class AuthConfigError(ValueError):
    """认证配置缺失或非法；服务应拒绝启动。"""


class Principal(BaseModel):
    """已验证的调用主体。

    Attributes:
        subject: 主体标识（服务账号、用户等）。
        tenant_id: 凭证绑定的租户，多租户隔离的权威来源。
        scopes: 授予的 scope；``*`` 表示全部。
    """

    model_config = ConfigDict(frozen=True)

    subject: str
    tenant_id: str
    scopes: frozenset[str] = frozenset()

    def has_scope(self, scope: str) -> bool:
        """是否持有 ``scope``（持有 ``*`` 视为持有全部 scope）。"""
        return SCOPE_ALL in self.scopes or scope in self.scopes


class Authenticator(Protocol):
    """认证器协议：校验请求头中的凭证，返回 :class:`Principal`。"""

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        """校验凭证。

        Raises:
            AuthenticationError: 凭证缺失或无效。
            PermissionDeniedError: 凭证有效但请求越权（例如租户头与凭证不符）。
        """
        ...


class ApiKeyEntry(BaseModel):
    """key 文件中的一条记录：只保存 key 的 SHA-256 摘要，不保存明文。

    字段只按 JSON 名（``tenantId``）解析，与网关的解析规则保持一致，同一份文件在
    两个服务中的校验结果相同。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tenant_id: str = Field(alias="tenantId", min_length=1)
    subject: str = Field(min_length=1)
    scopes: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE)

    @field_validator("sha256", mode="before")
    @classmethod
    def _normalize_digest(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - KNOWN_SCOPES)
        if unknown:
            raise ValueError(
                f"unknown scopes {unknown}; expected a subset of {sorted(KNOWN_SCOPES)}"
            )
        return value


class ApiKeyFile(BaseModel):
    """key 文件的顶层结构。"""

    model_config = ConfigDict(extra="forbid")

    keys: list[ApiKeyEntry]


def hash_api_key(key: str) -> str:
    """返回 key 的 SHA-256 十六进制摘要（key 文件中保存的值）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """生成一个新的随机 key（256 bit 熵）。"""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def load_api_keys(path: str | os.PathLike[str]) -> list[ApiKeyEntry]:
    """读取并校验 key 文件。

    Raises:
        AuthConfigError: 文件不可读，或内容不符合 key 文件格式。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthConfigError(f"cannot read API key file {path}: {exc}") from exc
    try:
        return ApiKeyFile.model_validate_json(raw).keys
    except ValidationError as exc:
        raise AuthConfigError(f"invalid API key file {path}: {exc}") from exc


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        value = next((v for k, v in headers.items() if k.lower() == lowered), None)
    return value


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


class HeaderAuthenticator:
    """``dev`` 模式：信任租户请求头，不校验任何凭证，只能用于本地开发。"""

    def __init__(self, tenant_header: str = TENANT_HEADER) -> None:
        self._tenant_header = tenant_header

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        tenant = (_header(headers, self._tenant_header) or "").strip()
        if not tenant:
            raise AuthenticationError(f"{self._tenant_header} header is required")
        return Principal(subject=f"dev:{tenant}", tenant_id=tenant, scopes=frozenset({SCOPE_ALL}))


class ApiKeyAuthenticator:
    """``apikey`` 模式：``Authorization: Bearer <key>``，key 绑定租户、主体与 scope。

    Raises:
        AuthConfigError: 构造时发现重复的 key 摘要或 id。
    """

    def __init__(self, entries: Iterable[ApiKeyEntry], tenant_header: str = TENANT_HEADER) -> None:
        by_digest: dict[str, ApiKeyEntry] = {}
        ids: set[str] = set()
        for entry in entries:
            if entry.sha256 in by_digest:
                raise AuthConfigError(f"duplicate API key digest (id={entry.id!r})")
            if entry.id in ids:
                raise AuthConfigError(f"duplicate API key id {entry.id!r}")
            by_digest[entry.sha256] = entry
            ids.add(entry.id)
        self._by_digest = by_digest
        self._tenant_header = tenant_header

    @classmethod
    def from_file(
        cls, path: str | os.PathLike[str], tenant_header: str = TENANT_HEADER
    ) -> ApiKeyAuthenticator:
        """从 key 文件构造认证器（见 :func:`load_api_keys`）。"""
        return cls(load_api_keys(path), tenant_header)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        key = _bearer_token(_header(headers, "Authorization"))
        if key is None:
            raise AuthenticationError("missing bearer credentials")
        digest = hash_api_key(key)
        entry = self._by_digest.get(digest)
        if entry is None or not hmac.compare_digest(entry.sha256, digest):
            raise AuthenticationError("invalid credentials")
        claimed = (_header(headers, self._tenant_header) or "").strip()
        if claimed and claimed != entry.tenant_id:
            raise PermissionDeniedError(
                f"{self._tenant_header} does not match the tenant bound to the credentials"
            )
        return Principal(
            subject=entry.subject, tenant_id=entry.tenant_id, scopes=frozenset(entry.scopes)
        )


def auth_mode_from_env(environ: Mapping[str, str] | None = None) -> str:
    """读取 ``FORGEMESH_AUTH_MODE``（缺省为 ``apikey``）。

    Raises:
        AuthConfigError: 取值不是 ``apikey`` 或 ``dev``。
    """
    env = os.environ if environ is None else environ
    mode = (env.get(AUTH_MODE_ENV) or DEFAULT_AUTH_MODE).strip().lower()
    if mode not in (AUTH_MODE_APIKEY, AUTH_MODE_DEV):
        raise AuthConfigError(
            f"unsupported {AUTH_MODE_ENV}={mode!r}; expected {AUTH_MODE_APIKEY!r} or {AUTH_MODE_DEV!r}"
        )
    return mode


def authenticator_from_env(environ: Mapping[str, str] | None = None) -> Authenticator:
    """按环境变量构造认证器。

    Raises:
        AuthConfigError: 模式非法；或 ``apikey`` 模式下没有配置（或无法读取）key 文件。
    """
    env = os.environ if environ is None else environ
    if auth_mode_from_env(env) == AUTH_MODE_DEV:
        return HeaderAuthenticator()
    path = (env.get(API_KEYS_FILE_ENV) or "").strip()
    if not path:
        raise AuthConfigError(
            f"{API_KEYS_FILE_ENV} must point to an API key file when {AUTH_MODE_ENV}="
            f"{AUTH_MODE_APIKEY} (the default); use {AUTH_MODE_ENV}={AUTH_MODE_DEV} only for "
            "local development"
        )
    return ApiKeyAuthenticator.from_file(path)


def _keygen(args: argparse.Namespace) -> int:
    key = generate_api_key()
    entry = ApiKeyEntry(
        id=args.id or f"{args.tenant}-{args.subject}",
        sha256=hash_api_key(key),
        tenantId=args.tenant,
        subject=args.subject,
        scopes=tuple(args.scope) if args.scope else (SCOPE_READ, SCOPE_WRITE),
    )
    print("API key (store it in a secret manager; it is not shown again):")
    print(key)
    print('Key file entry (append it to the "keys" array):')
    print(json.dumps(entry.model_dump(by_alias=True, mode="json"), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口：``keygen`` 生成新 key 及对应的 key 文件条目。"""
    parser = argparse.ArgumentParser(prog="python -m forgemesh.controlplane.auth")
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="generate an API key bound to a tenant")
    keygen.add_argument("--tenant", required=True, help="tenant bound to the key")
    keygen.add_argument("--subject", required=True, help="subject (service account or user)")
    keygen.add_argument("--id", help="entry id (default: <tenant>-<subject>)")
    keygen.add_argument(
        "--scope",
        action="append",
        choices=sorted(KNOWN_SCOPES),
        help="granted scope, repeatable (default: read and write)",
    )
    args = parser.parse_args(argv)
    try:
        return _keygen(args)
    except ValidationError as exc:
        print(f"invalid key parameters: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
