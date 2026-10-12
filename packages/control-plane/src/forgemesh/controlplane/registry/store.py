"""SQLite 资源注册表实现。

对齐 ``forgemesh.core.registry.ResourceRegistry`` 契约，以 SQLite 作为权威状态
存储后端（阶段 P1）。所有异步方法通过 :func:`asyncio.to_thread` 包装同步 sqlite3
调用，适配事件循环环境；同一实例共享单一连接，所有数据库访问经实例级锁串行化
（sqlite3 连接级事务不可跨线程交错，否则一方 rollback 会连带丢弃另一方未提交的写入）。

并发语义
--------
* ``put(expected_version=None)`` → **Create**，``resource_version`` 从 0 起；
  同名资源已存在抛 :class:`~forgemesh.core.errors.ConflictError`。
* ``put(expected_version=<str>)`` → **Update-CAS**，与当前 ``resource_version``
  不一致（或资源不存在）抛 :class:`~forgemesh.core.errors.ConflictError`；
  成功后 ``resource_version`` 递增 1。
* ``resource_version`` 在 DB 中为 ``INTEGER``，对外的
  ``metadata.resourceVersion`` 为字符串（对齐契约 ``Optional[str]``）。

受控删除（finalizer 语义，对齐 architecture.md §5.3/§12.4 与 development-plan
第 7 章）
------------------------------------------------------------------------
* ``resources`` 表以元数据列 ``phase`` 持久化生命周期阶段（与 ``generation``
  等并列，由强类型子类的 ``status.phase`` 提取；基类 ``Resource`` 无 phase，
  列为 ``NULL``），以 ``finalizers`` 列镜像 payload 中的 finalizer 列表。
* :meth:`request_delete` 为删除入口：finalizers 非空（或 phase 属于有副作用
  态，自动追加默认 finalizer）→ 置 ``phase='Terminating'`` 持久化并交由协调器
  驱动「受控释放 → 清空 finalizer → 物理删除」；否则直接物理 DELETE
  （与既有 ``delete()`` 行为一致，向后兼容）。
* ``put()`` 的 Update-CAS 路径按 kind 接入权威状态机（``INFERENCE_STATE_MACHINE``
  等）校验 phase 转移：非法跳变抛
  :class:`~forgemesh.core.errors.InvalidStateTransition`；首次创建（无旧记录）
  与不受管 kind 跳过校验。

Allocation 持久化
------------------
``allocations`` 表持久化 :class:`~forgemesh.resource.allocation.Allocation`
（``Active | Released | Expired``），以**部分唯一索引**
``idx_allocations_one_active_per_resource`` 兜底「同一 ``resource_id`` 至多一个
Active 分配」；``release_allocation`` 走 ``ALLOCATION_STATE_MACHINE`` 校验。

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
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar
from uuid import uuid4

from forgemesh.core.errors import ConflictError, NotFoundError, PreconditionFailedError
from forgemesh.core.objects import Resource
from forgemesh.core.registry import ResourceRegistry
from forgemesh.resource.allocation import Allocation, AllocationStatus
from forgemesh.resource.inference import InferenceService
from forgemesh.resource.states import ALLOCATION_STATE_MACHINE, INFERENCE_STATE_MACHINE
from pydantic import ValidationError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: kind → 权威状态机映射；未登记的 kind 视为不受管，put() 跳过转移校验。
_KIND_STATE_MACHINES = {
    "InferenceService": INFERENCE_STATE_MACHINE,
}

#: kind → 强类型资源模型注册表：读回时按 kind 反序列化，保留 spec / status；
#: 未登记 kind 回退基类并投影到基类已知字段（兼容第三方 kind / 旧 payload）。
_KIND_MODELS: dict[str, type[Resource]] = {
    "InferenceService": InferenceService,
}

#: 默认受控释放 finalizer：由协调器在 ReleaseProvider 成功释放后移除。
DEFAULT_RELEASE_FINALIZER = "forgemesh.io/resource-release"

#: 视为「有外部副作用」的 phase：request_delete 时自动追加默认 finalizer，
#: 先受控释放 GPU/实例再物理删除。Pending/Scheduling/Failed/Unknown 无外部
#: 副作用，直接物理删除（向后兼容）。
SIDE_EFFECT_PHASES = frozenset({"Ready", "Degraded", "Updating", "Deploying", "Verifying"})


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
    phase TEXT,
    finalizers TEXT NOT NULL DEFAULT '[]',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, kind, namespace, name)
);
CREATE INDEX IF NOT EXISTS idx_resources_kind ON resources(tenant_id, kind);
CREATE INDEX IF NOT EXISTS idx_resources_labels ON resources(tenant_id, kind, namespace);
CREATE TABLE IF NOT EXISTS allocations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    lease_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_allocations_tenant ON allocations(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_allocations_one_active_per_resource
    ON allocations(resource_id) WHERE status = 'Active';
"""


@dataclass(frozen=True)
class RequestDeleteResult:
    """:meth:`SQLiteRegistry.request_delete` 的结果。

    Attributes:
        deleted: ``True`` 表示已物理删除；``False`` 表示进入 ``Terminating``
            受控释放流程（等待协调器清空 finalizer 后删除）。
        finalizers: 删除请求受理时资源持有的 finalizer 列表（物理删除时为空）。
    """

    deleted: bool
    finalizers: builtins.list[str] = field(default_factory=list)


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
        self._lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _run(self, fn: Callable[[], _T]) -> _T:
        """在工作线程中持锁执行一段同步数据库操作（读写均串行化）。"""

        def _locked() -> _T:
            with self._lock:
                return fn()

        return await asyncio.to_thread(_locked)

    def _init_schema(self) -> None:
        """创建表与索引（幂等），并对存量库做列迁移。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate_legacy_resources()
        self._ensure_identity_index()
        self._conn.commit()

    def _ensure_identity_index(self) -> None:
        """建立把 NULL namespace 视为相等的身份唯一索引。

        SQLite 主键允许 NULL 且 NULL 互不相等，仅靠主键无法阻止 namespace 为空的
        资源重名。存量库已存在重复记录时拒绝打开，避免静默删除数据。
        """
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_resources_identity'"
        ).fetchone()
        if exists is not None:
            return
        duplicate = self._conn.execute(
            "SELECT tenant_id, kind, COALESCE(namespace, '') AS ns, name, COUNT(*) AS n "
            "FROM resources GROUP BY tenant_id, kind, ns, name HAVING n > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise PreconditionFailedError(
                "duplicate resources must be removed before upgrading: "
                f"tenant_id={duplicate['tenant_id']} kind={duplicate['kind']} "
                f"namespace={duplicate['ns']!r} name={duplicate['name']} "
                f"count={duplicate['n']}",
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX ux_resources_identity "
            "ON resources(tenant_id, kind, COALESCE(namespace, ''), name)"
        )

    def _migrate_legacy_resources(self) -> None:
        """存量库幂等迁移：以 ``PRAGMA table_info`` 探测后补 ``phase`` /
        ``finalizers`` 列（内存库 ``:memory:`` 每次新建无此问题，但保持幂等）。

        旧结构库文件（v1.0.0 及之前）无这两列；``ALTER TABLE ADD COLUMN``
        幂等执行，重复打开不报错。
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(resources)")}
        if "phase" not in columns:
            self._conn.execute("ALTER TABLE resources ADD COLUMN phase TEXT")
            logger.info("migrated legacy resources table: added column 'phase'")
        if "finalizers" not in columns:
            self._conn.execute(
                "ALTER TABLE resources ADD COLUMN finalizers TEXT NOT NULL DEFAULT '[]'"
            )
            logger.info("migrated legacy resources table: added column 'finalizers'")

    @staticmethod
    def _extract_phase(resource: Resource) -> str | None:
        """从资源对象提取生命周期 phase。

        仅受管 kind 的强类型子类（如 ``InferenceService``）在 ``status.phase``
        上携带 phase；基类 ``Resource`` 返回 ``None``。
        """
        status = getattr(resource, "status", None)
        phase = getattr(status, "phase", None)
        return str(phase) if phase is not None else None

    @staticmethod
    def _validate_phase_transition(kind: str, current: str | None, target: str | None) -> None:
        """按 kind 接入权威状态机校验 ``current → target`` 转移。

        跳过校验的情形（均不产生副作用）：

        * kind 不在 :data:`_KIND_STATE_MACHINES`（不受管 kind）；
        * ``current`` / ``target`` 任一为 ``None``（旧 payload 无 phase，或
          写入方不携带 phase 的基类资源——向后兼容）；
        * ``current == target``（同状态重写，非转移）。

        Raises:
            InvalidStateTransition: 非法跳变（如 ``Pending → Ready``）。
        """
        machine = _KIND_STATE_MACHINES.get(kind)
        if machine is None or current is None or target is None or current == target:
            return
        machine.validate(current, target)

    @staticmethod
    def _label_path(key: str) -> str:
        """把标签键转换为 SQLite JSON 路径（键整体加双引号，兼容 ``.`` 与 ``/``）。"""
        if '"' in key or "\\" in key:
            raise ValueError(f"invalid label key: {key!r}")
        return f'$.metadata.labels."{key}"'

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
        """将数据库行反序列化为 :class:`Resource`。

        受管 kind（登记于 :data:`_KIND_MODELS`）优先按强类型子类解析（保留
        ``spec`` / ``status``）；解析失败（如基类形状的 payload——部分调用方
        与既有测试直接以基类 ``Resource`` 写入）或 kind 未登记时，回退基类并
        投影到基类已知字段（丢弃未知扩展字段），保证旧 payload / 第三方 kind
        的读回兼容。
        """
        body = row["body"]
        data = json.loads(body)
        if isinstance(data, dict):
            model = _KIND_MODELS.get(data.get("kind", ""))
            if model is not None:
                try:
                    return model.model_validate_json(body)
                except ValidationError:
                    pass  # 基类形状 payload：回退基类投影
        known_fields = Resource.model_fields
        return Resource.model_validate(
            {key: value for key, value in data.items() if key in known_fields}
        )

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

        return await self._run(_get)

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
        phase = self._extract_phase(resource)
        finalizers_json = json.dumps(list(resource.finalizers))

        def _put() -> Resource:
            cur = self._conn.cursor()
            try:
                if expected_version is None:
                    # ---- Create ----
                    # 首次创建无旧记录，跳过状态机校验（初始 phase 任意合法值）。
                    new_version = 0
                    stored = resource.model_copy(deep=True)
                    stored.metadata.resourceVersion = str(new_version)
                    body = stored.model_dump_json(by_alias=True)
                    cur.execute(
                        "INSERT INTO resources "
                        "(tenant_id, kind, namespace, name, uid, "
                        " resource_version, generation, observed_generation, "
                        " phase, finalizers, body) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            tenant_id,
                            kind,
                            namespace,
                            name,
                            meta.uid,
                            new_version,
                            generation,
                            observed_generation,
                            phase,
                            finalizers_json,
                            body,
                        ),
                    )
                    self._conn.commit()
                    return stored
                # ---- Update-CAS ----
                expected_int = int(expected_version)
                # 状态机接线：按 kind 校验 phase 转移（旧记录无 phase / 不受管
                # kind / 同状态重写时跳过；非法跳变抛 InvalidStateTransition）。
                old_row = cur.execute(
                    "SELECT phase FROM resources "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                    (tenant_id, kind, namespace, name),
                ).fetchone()
                if old_row is not None:
                    self._validate_phase_transition(kind, old_row["phase"], phase)
                new_version = expected_int + 1
                stored = resource.model_copy(deep=True)
                stored.metadata.resourceVersion = str(new_version)
                body = stored.model_dump_json(by_alias=True)
                cur.execute(
                    "UPDATE resources SET uid=?, resource_version=?, generation=?, "
                    " observed_generation=?, phase=?, finalizers=?, body=?, "
                    " updated_at=datetime('now') "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=? "
                    " AND resource_version=?",
                    (
                        meta.uid,
                        new_version,
                        generation,
                        observed_generation,
                        phase,
                        finalizers_json,
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

        return await self._run(_put)

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

        标签过滤在 SQL 中先于 ``LIMIT`` 执行，保证每页返回的都是匹配项。
        ``watch=True`` 在阶段 P1 暂未实现，抛 :class:`NotImplementedError`。

        Raises:
            ValueError: ``limit`` 小于 1，或标签键包含双引号 / 反斜杠。
        """
        if watch:
            raise NotImplementedError("watch stream is not implemented in phase P1")
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        label_paths = [
            (self._label_path(key), value) for key, value in sorted((labels or {}).items())
        ]

        def _list() -> list[Resource]:
            sql = "SELECT body FROM resources WHERE tenant_id=? AND kind=?"
            params: list[Any] = [tenant_id, kind]
            if namespace is not None:
                sql += " AND namespace IS ?"
                params.append(namespace)
            for path, value in label_paths:
                sql += " AND json_extract(body, ?) = ?"
                params.extend([path, value])
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
            return [self._row_to_resource(r) for r in rows]

        return await self._run(_list)

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

        await self._run(_delete)

    async def request_delete(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
        actor: str | None = None,
        correlation_id: str | None = None,
    ) -> RequestDeleteResult:
        """受控删除入口：finalizer 非空 → ``Terminating``；为空 → 物理 DELETE。

        规则（对齐 architecture.md §5.3/§12.4 与 development-plan 第 7 章删除
        闭环；``phase`` 存于 ``resources.phase`` 元数据列，权威状态机校验）：

        * 资源已处于 ``Terminating`` → 幂等返回（不重复递增版本）；
        * finalizers 非空 → 持久化 ``phase='Terminating'``（非法转移抛
          :class:`~forgemesh.core.errors.InvalidStateTransition`），返回
          ``deleted=False``，由协调器驱动受控释放；
        * finalizers 为空且 phase 属于有副作用态（:data:`SIDE_EFFECT_PHASES`，
          如 ``Ready``）→ 自动追加默认 finalizer
          :data:`DEFAULT_RELEASE_FINALIZER` 后同上；
        * 其余（Pending/Scheduling/Failed/Unknown/无 phase）无外部副作用，
          直接物理 DELETE（与 :meth:`delete` 行为一致，向后兼容）。

        payload 与 ``finalizers`` 列双写保持一致；子类 payload 的
        ``status.phase``（若存在）同步为 ``Terminating``，基类 payload 不引入
        ``status`` 子树。

        Args:
            kind: 资源类型标识。
            namespace: 命名空间（可为 ``None``）。
            name: 资源名。
            tenant_id: 租户标识（多租户边界）。
            actor: 操作发起者标识（审计用）。
            correlation_id: 关联追踪标识（审计用）。

        Returns:
            :class:`RequestDeleteResult`：``deleted=True`` 已物理删除；
            ``deleted=False`` 已进入 ``Terminating``。

        Raises:
            NotFoundError: 目标资源不存在（或跨租户不可见）。
            InvalidStateTransition: 当前 phase 不允许转移到 ``Terminating``。
            ConflictError: 读取后资源被并发修改（``resource_version`` 已变化），
                调用方应重新读取后重试。
        """
        terminating = "Terminating"

        def _request_delete() -> RequestDeleteResult:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT body, phase, finalizers, resource_version FROM resources "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                (tenant_id, kind, namespace, name),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"resource not found: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            phase = row["phase"]
            read_version = row["resource_version"]
            current_finalizers = json.loads(row["finalizers"] or "[]")
            if phase == terminating:
                # 幂等：删除请求已受理，仍在受控释放流程中。
                return RequestDeleteResult(False, list(current_finalizers))
            effective = list(current_finalizers)
            if not effective and phase in SIDE_EFFECT_PHASES:
                effective.append(DEFAULT_RELEASE_FINALIZER)
            if not effective:
                # 无 finalizer 且无外部副作用 → 直接物理删除（向后兼容）。
                # 以读到的版本为条件：并发写入可能刚追加了 finalizer。
                cur.execute(
                    "DELETE FROM resources "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=? "
                    " AND resource_version=?",
                    (tenant_id, kind, namespace, name, read_version),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    raise ConflictError(
                        f"request_delete raced with concurrent write: kind={kind} "
                        f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                    )
                self._conn.commit()
                return RequestDeleteResult(True, [])
            # finalizer 非空（或自动追加）→ 受控删除：置 Terminating。
            self._validate_phase_transition(kind, phase, terminating)
            new_version = read_version + 1
            body = json.loads(row["body"])
            body["finalizers"] = effective
            # 仅当 payload 已携带 status 子树（强类型子类）时同步 phase；
            # 基类 payload 不引入 status，保证读回基类 Resource 可解析。
            if isinstance(body.get("status"), dict):
                body["status"]["phase"] = terminating
            # payload 内的 metadata.resourceVersion 与 DB 列保持同步（列权威），
            # 否则读回对象的 CAS 依据会与列漂移。
            metadata = body.get("metadata")
            if isinstance(metadata, dict):
                metadata["resourceVersion"] = str(new_version)
            cur.execute(
                "UPDATE resources SET body=?, phase=?, finalizers=?, "
                " resource_version=?, updated_at=datetime('now') "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=? "
                " AND resource_version=?",
                (
                    json.dumps(body, ensure_ascii=False),
                    terminating,
                    json.dumps(effective),
                    new_version,
                    tenant_id,
                    kind,
                    namespace,
                    name,
                    read_version,
                ),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ConflictError(
                    f"request_delete raced with concurrent write: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            self._conn.commit()
            logger.info(
                "delete requested: kind=%s name=%s tenant_id=%s → Terminating "
                "(finalizers=%s, actor=%s, correlation_id=%s)",
                kind,
                name,
                tenant_id,
                effective,
                actor,
                correlation_id,
            )
            return RequestDeleteResult(False, effective)

        return await self._run(_request_delete)

    # ------------------------------------------------------------------ #
    # Allocation 持久化（development-plan 7.2 DDL）
    # ------------------------------------------------------------------ #

    async def put_allocation(self, allocation: Allocation) -> str:
        """插入分配记录，返回新生成的分配 id。

        同一 ``resource_id`` 已存在 ``Active`` 分配时，由部分唯一索引
        ``idx_allocations_one_active_per_resource`` 兜底拒绝；重复
        ``idempotency_key`` 由唯一约束拒绝；两者均转
        :class:`~forgemesh.core.errors.ConflictError`。

        Args:
            allocation: 分配记录（``status`` 默认 ``Active``）。

        Returns:
            新分配的 ``id``（``uuid4().hex``）。
        """

        def _put_allocation() -> str:
            allocation_id = uuid4().hex
            try:
                self._conn.execute(
                    "INSERT INTO allocations "
                    "(id, tenant_id, resource_id, status, idempotency_key, lease_until) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        allocation_id,
                        allocation.tenant_id,
                        allocation.resource_id,
                        allocation.status.value,
                        allocation.idempotency_key,
                        (
                            allocation.lease_until.isoformat()
                            if allocation.lease_until is not None
                            else None
                        ),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                message = str(exc)
                if "idempotency_key" in message:
                    raise ConflictError(
                        f"duplicate allocation idempotency_key: {allocation.idempotency_key!r}",
                    ) from exc
                raise ConflictError(
                    "resource already has an active allocation: "
                    f"resource_id={allocation.resource_id!r}",
                ) from exc
            return allocation_id

        return await self._run(_put_allocation)

    async def get_active_allocation(
        self,
        resource_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        """读取 ``resource_id`` 当前唯一的 Active 分配；不存在返回 ``None``。

        强制以 ``tenant_id`` 过滤（与 resources 一致的多租户边界）。
        """

        def _get_active() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM allocations WHERE tenant_id=? AND resource_id=? AND status=?",
                (tenant_id, resource_id, AllocationStatus.ACTIVE.value),
            ).fetchone()
            return dict(row) if row is not None else None

        return await self._run(_get_active)

    async def release_allocation(
        self,
        allocation_id: str,
        tenant_id: str,
        reason: str | None = None,
    ) -> None:
        """将分配 ``Active → Released``（走 ``ALLOCATION_STATE_MACHINE`` 校验）。

        Args:
            allocation_id: 分配 id。
            tenant_id: 租户标识（多租户边界，强制过滤）。
            reason: 释放原因（P1 仅记录日志，供审计追溯）。

        Raises:
            NotFoundError: 分配不存在（或跨租户不可见）。
            InvalidStateTransition: 当前状态非 ``Active``（终态不可逆）。
            ConflictError: 读取后状态被并发修改（如已被另一方释放或过期）。
        """

        def _release() -> None:
            row = self._conn.execute(
                "SELECT status FROM allocations WHERE tenant_id=? AND id=?",
                (tenant_id, allocation_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"allocation not found: id={allocation_id} tenant_id={tenant_id}",
                )
            ALLOCATION_STATE_MACHINE.validate(row["status"], AllocationStatus.RELEASED.value)
            cur = self._conn.execute(
                "UPDATE allocations SET status=?, released_at=datetime('now') "
                "WHERE tenant_id=? AND id=? AND status=?",
                (AllocationStatus.RELEASED.value, tenant_id, allocation_id, row["status"]),
            )
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ConflictError(
                    f"allocation changed concurrently: id={allocation_id} tenant_id={tenant_id}",
                )
            self._conn.commit()
            if reason is not None:
                logger.debug(
                    "allocation released: id=%s tenant_id=%s reason=%s",
                    allocation_id,
                    tenant_id,
                    reason,
                )

        await self._run(_release)

    async def list_allocations(
        self,
        tenant_id: str,
    ) -> builtins.list[dict[str, Any]]:
        """列出租户的全部分配记录（含终态），强制租户过滤。"""

        def _list_allocations() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM allocations WHERE tenant_id=? ORDER BY created_at, id",
                (tenant_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._run(_list_allocations)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """关闭数据库连接。"""
        await self._run(self._conn.close)


__all__ = [
    "DEFAULT_RELEASE_FINALIZER",
    "SIDE_EFFECT_PHASES",
    "RequestDeleteResult",
    "SQLiteRegistry",
]
