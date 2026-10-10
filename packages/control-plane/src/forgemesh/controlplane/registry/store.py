"""SQLite 资源注册表实现。

对齐 ``forgemesh.core.registry.ResourceRegistry`` 契约，以 SQLite 作为权威状态
存储后端（阶段 P1）。所有异步方法通过 :func:`asyncio.to_thread` 包装同步 sqlite3
调用，适配事件循环环境。

并发语义
--------
* ``put(expected_version=None)`` → **Create**，``resource_version`` 从 0 起；
  同名资源已存在抛 :class:`~forgemesh.core.errors.ConflictError`。
* ``put(expected_version=<str>)`` → **Update-CAS**，与当前 ``resource_version``
  不一致（或资源不存在）抛 :class:`~forgemesh.core.errors.ConflictError`；
  成功后 ``resource_version`` 递增 1。
* ``resource_version`` 在 DB 中为 ``INTEGER``，对外的
  ``metadata.resourceVersion`` 为字符串（对齐契约 ``Optional[str]``）。

租户隔离
--------
所有查询均以 ``tenant_id`` 作为首要过滤条件；跨租户访问一律视为
:class:`~forgemesh.core.errors.NotFoundError`，不泄露存在性。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import json
import sqlite3
from typing import Any

from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.core.objects import Resource
from forgemesh.core.registry import ResourceRegistry

#: 建表与建索引 DDL（幂等执行）。
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS resources (
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT,
    name TEXT NOT NULL,
    uid TEXT,
    resource_version INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    observed_generation INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, kind, namespace, name)
);
CREATE INDEX IF NOT EXISTS idx_resources_kind ON resources(tenant_id, kind);
CREATE INDEX IF NOT EXISTS idx_resources_labels ON resources(tenant_id, kind, namespace);
"""


class SQLiteRegistry(ResourceRegistry):
    """基于 SQLite 的资源注册表。

    Args:
        db_path: SQLite 数据库文件路径，默认 ``:memory:``（内存库，单测友好）。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        # check_same_thread=False：asyncio.to_thread 会在不同线程执行 sqlite 调用。
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _init_schema(self) -> None:
        """创建表与索引（幂等）。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    @staticmethod
    def _encode_token(namespace: str | None, name: str) -> str:
        """将分页游标 ``(namespace, name)`` 编码为 opaque base64 字符串。"""
        payload = json.dumps({"ns": namespace, "name": name}, ensure_ascii=False)
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_token(token: str) -> tuple[str | None, str]:
        """解码分页游标，返回 ``(namespace, name)``。"""
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
            payload = json.loads(raw)
            return payload["ns"], payload["name"]
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid continue_token: {token!r}") from exc

    @staticmethod
    def _row_to_resource(row: sqlite3.Row) -> Resource:
        """将数据库行反序列化为 :class:`Resource`。"""
        return Resource.model_validate_json(row["body"])

    # ------------------------------------------------------------------ #
    # ResourceRegistry 契约实现
    # ------------------------------------------------------------------ #

    async def get(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
    ) -> Resource:
        """读取单个资源；不存在或跨租户时抛 :class:`NotFoundError`。"""

        def _get() -> Resource:
            row = self._conn.execute(
                "SELECT body FROM resources "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                (tenant_id, kind, namespace, name),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"resource not found: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            return self._row_to_resource(row)

        return await asyncio.to_thread(_get)

    async def put(
        self,
        resource: Resource,
        expected_version: str | None = None,
        actor: str | None = None,
        correlation_id: str | None = None,
    ) -> Resource:
        """写入（Create 或 Update-CAS）资源，返回写入后的最新对象。

        * ``expected_version`` 为 ``None`` → Create；同名资源已存在抛 ConflictError。
        * ``expected_version`` 非 ``None`` → Update-CAS；版本不匹配或资源不存在抛
          ConflictError。成功后 ``resource_version`` 递增。

        ``actor`` / ``correlation_id`` 保留用于审计（P1 暂不落审计表）。
        """
        meta = resource.metadata
        tenant_id = meta.tenant_id
        if tenant_id is None:
            raise ConflictError("metadata.tenant_id (tenantId) must not be empty")
        namespace = meta.namespace
        name = meta.name
        kind = resource.kind
        generation = meta.generation
        observed_generation = meta.observed_generation

        def _put() -> Resource:
            cur = self._conn.cursor()
            try:
                if expected_version is None:
                    # ---- Create ----
                    new_version = 0
                    stored = resource.model_copy(deep=True)
                    stored.metadata.resourceVersion = str(new_version)
                    body = stored.model_dump_json(by_alias=True)
                    cur.execute(
                        "INSERT INTO resources "
                        "(tenant_id, kind, namespace, name, uid, "
                        " resource_version, generation, observed_generation, body) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            tenant_id,
                            kind,
                            namespace,
                            name,
                            meta.uid,
                            new_version,
                            generation,
                            observed_generation,
                            body,
                        ),
                    )
                    self._conn.commit()
                    return stored
                # ---- Update-CAS ----
                expected_int = int(expected_version)
                new_version = expected_int + 1
                stored = resource.model_copy(deep=True)
                stored.metadata.resourceVersion = str(new_version)
                body = stored.model_dump_json(by_alias=True)
                cur.execute(
                    "UPDATE resources SET uid=?, resource_version=?, generation=?, "
                    " observed_generation=?, body=?, updated_at=datetime('now') "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=? "
                    " AND resource_version=?",
                    (
                        meta.uid,
                        new_version,
                        generation,
                        observed_generation,
                        body,
                        tenant_id,
                        kind,
                        namespace,
                        name,
                        expected_int,
                    ),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    raise ConflictError(
                        f"CAS update failed: expected_version={expected_version!r} "
                        f"does not match current or resource does not exist "
                        f"(kind={kind} namespace={namespace!r} name={name})",
                    )
                self._conn.commit()
                return stored
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ConflictError(
                    f"resource already exists: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                ) from exc

        return await asyncio.to_thread(_put)

    async def list(
        self,
        kind: str,
        tenant_id: str,
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
        watch: bool = False,
        continue_token: str | None = None,
        limit: int = 100,
    ) -> builtins.list[Resource]:
        """按 ``kind`` + 可选 ``namespace`` / ``labels`` 过滤列出资源（分页）。

        ``watch=True`` 在阶段 P1 暂未实现，抛 :class:`NotImplementedError`。
        """
        if watch:
            raise NotImplementedError("watch stream is not implemented in phase P1")

        def _list() -> list[Resource]:
            sql = "SELECT body FROM resources WHERE tenant_id=? AND kind=?"
            params: list[Any] = [tenant_id, kind]
            if namespace is not None:
                sql += " AND namespace IS ?"
                params.append(namespace)
            if continue_token is not None:
                cursor_ns, cursor_name = self._decode_token(continue_token)
                cursor_ns_key = cursor_ns if cursor_ns is not None else ""
                # 按 (COALESCE(namespace,''), name) 升序游标推进
                sql += (
                    " AND (COALESCE(namespace, '') > ?"
                    " OR (COALESCE(namespace, '') = ? AND name > ?))"
                )
                params.extend([cursor_ns_key, cursor_ns_key, cursor_name])
            sql += " ORDER BY COALESCE(namespace, ''), name LIMIT ?"
            params.append(limit)
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            results = [self._row_to_resource(r) for r in rows]
            if labels:
                results = [
                    res
                    for res in results
                    if all(res.metadata.labels.get(k) == v for k, v in labels.items())
                ]
            return results

        return await asyncio.to_thread(_list)

    async def delete(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
        actor: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """删除资源；不存在或跨租户时抛 :class:`NotFoundError`。"""

        def _delete() -> None:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM resources WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                (tenant_id, kind, namespace, name),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise NotFoundError(
                    f"resource not found: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            self._conn.commit()

        await asyncio.to_thread(_delete)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """关闭数据库连接。"""
        await asyncio.to_thread(self._conn.close)


__all__ = ["SQLiteRegistry"]
