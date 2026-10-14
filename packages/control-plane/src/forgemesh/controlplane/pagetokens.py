"""签名的分页游标，资源列表与审计记录列表共用。

游标格式为 ``<载荷>.<签名>``：载荷是本页最后一项的位置加格式版本 ``v`` 的紧凑 JSON 的
base64url 编码（无填充），签名是对「载荷 + ``.`` + 上下文」计算的 HMAC-SHA256（同样
base64url 编码）。上下文是 :func:`page_scope` 给出的查询条件（租户与过滤条件，不含
``limit``），由各个列表决定：资源列表（:meth:`SQLiteRegistry.list_page
<forgemesh.controlplane.registry.SQLiteRegistry.list_page>`）是租户、kind、namespace 与标签
选择器，审计记录列表（:meth:`AuditLog.list_page
<forgemesh.controlplane.audit.AuditLog.list_page>`）是列表名、租户与 correlationId。两种
上下文的字段不同，游标不能在两种列表之间混用。

签名密钥是 ``registry_meta`` 表 ``page_token_key`` 行中的 32 个随机字节（hex），首次打开
数据库时生成并随数据库保存：控制平面重启后游标仍然有效，来自另一个数据库的游标被拒绝。
游标只记录位置，不是访问凭据：查询始终限定在调用者的租户内。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any

__all__ = ["InvalidPageTokenError", "PageTokenSigner", "load_page_token_key", "page_scope"]

#: ``registry_meta`` 中保存签名密钥（hex）的行。
_PAGE_TOKEN_KEY_NAME = "page_token_key"

#: 游标载荷的格式版本。
_PAGE_TOKEN_VERSION = 1

_META_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS registry_meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_INVALID_PAGE_TOKEN = (
    "invalid page token: it is malformed, was modified, or was issued for "
    "a different list, tenant or filter set"
)


class InvalidPageTokenError(ValueError):
    """分页游标无效：无法解析、被篡改，或签发给了另一个列表、租户或另一组过滤条件。"""


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_page_token_key(conn: sqlite3.Connection) -> bytes:
    """读取签名密钥；库中还没有时生成并持久化（需要时先创建 ``registry_meta`` 表）。

    多个进程同时打开同一个新库时，``INSERT OR IGNORE`` 保证只有先写入的密钥生效，
    各方随后读到的都是同一个值。连接由多个存储共用时，调用方须持有它的锁。
    """
    conn.execute(_META_SCHEMA_SQL)
    query = "SELECT value FROM registry_meta WHERE name=?"
    row = conn.execute(query, (_PAGE_TOKEN_KEY_NAME,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO registry_meta (name, value) VALUES (?, ?)",
            (_PAGE_TOKEN_KEY_NAME, secrets.token_hex(32)),
        )
        conn.commit()
        row = conn.execute(query, (_PAGE_TOKEN_KEY_NAME,)).fetchone()
    return bytes.fromhex(row[0])


def page_scope(conditions: Mapping[str, Any]) -> str:
    """游标绑定的上下文：查询条件的紧凑 JSON（键排序，与给出的顺序无关）。"""
    return json.dumps(conditions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class PageTokenSigner:
    """用一个数据库的签名密钥签发与校验游标。

    Args:
        key: :func:`load_page_token_key` 读到的密钥。
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _sign(self, payload: str, scope: str) -> str:
        message = f"{payload}.{scope}".encode()
        return _b64encode(hmac.new(self._key, message, hashlib.sha256).digest())

    def encode(self, position: Mapping[str, Any], scope: str) -> str:
        """把本页最后一项的位置编码为绑定 ``scope`` 的签名游标。"""
        data = {"v": _PAGE_TOKEN_VERSION, **position}
        payload = _b64encode(
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        return f"{payload}.{self._sign(payload, scope)}"

    def decode(self, token: str, scope: str, fields: Mapping[str, type]) -> tuple[Any, ...]:
        """校验签名并解码游标，按 ``fields`` 的顺序返回位置中各字段的值。

        Args:
            token: :meth:`encode` 签发的游标。
            scope: 当前查询的上下文，须与签发时相同。
            fields: 位置的字段名 → 期望的类型。

        Raises:
            InvalidPageTokenError: 游标无法解析、被篡改、属于另一个上下文 ``scope``，或位置
                缺少 ``fields`` 中的字段（或类型不符）。
        """
        # 签发的游标只含 base64url 字符与 "."；先排除非 ASCII 输入，compare_digest 才能比较 str
        if not token.isascii():
            raise InvalidPageTokenError(_INVALID_PAGE_TOKEN)
        payload, _, signature = token.partition(".")
        if not hmac.compare_digest(signature, self._sign(payload, scope)):
            raise InvalidPageTokenError(_INVALID_PAGE_TOKEN)
        try:
            position = json.loads(_b64decode(payload))
        except ValueError as exc:
            raise InvalidPageTokenError(_INVALID_PAGE_TOKEN) from exc
        if not (
            isinstance(position, dict)
            and position.get("v") == _PAGE_TOKEN_VERSION
            and all(isinstance(position.get(name), kind) for name, kind in fields.items())
        ):
            raise InvalidPageTokenError(_INVALID_PAGE_TOKEN)
        return tuple(position[name] for name in fields)
