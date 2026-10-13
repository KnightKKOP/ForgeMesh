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
* :meth:`SQLiteRegistry.update_status` → **status 子资源**：只修改状态条件、
  phase 与 ``observedGeneration``，不递增 ``generation``，可选 CAS；合并后
  没有变化时不写库。
* :meth:`SQLiteRegistry.update_spec` → **期望状态**：整体替换 ``spec`` / labels /
  annotations，可选 uid 与版本 CAS；只有 ``spec`` 变化时递增 ``generation``，
  ``Terminating`` 的资源拒绝更新；没有变化时不写库。

受控删除（finalizer 语义，对齐 architecture.md §5.3/§12.4 与 development-plan
第 7 章）
------------------------------------------------------------------------
* ``resources`` 表以元数据列 ``phase`` 持久化生命周期阶段（与 ``generation``
  等并列，由强类型子类的 ``status.phase`` 提取；基类 ``Resource`` 无 phase，
  列为 ``NULL``），以 ``finalizers`` 列镜像 payload 中的 finalizer 列表。
* :meth:`request_delete` 为删除入口：finalizers 非空（或 phase 属于有副作用
  态、或资源持有 Active 分配，自动追加默认 finalizer）→ 置 ``phase='Terminating'``
  持久化并交由协调器驱动「受控释放 → 清空 finalizer → 释放分配 → 物理删除」；
  否则直接物理 DELETE（与既有 ``delete()`` 行为一致）。
* ``put()`` 的 Update-CAS 路径按 kind 接入权威状态机（``INFERENCE_STATE_MACHINE``
  等）校验 phase 转移：非法跳变抛
  :class:`~forgemesh.core.errors.InvalidStateTransition`；首次创建（无旧记录）
  与不受管 kind 跳过校验。

Allocation 持久化
------------------
``allocations`` 表持久化 :class:`~forgemesh.resource.allocation.Allocation`
（``Active | Released | Expired``），``resource_id`` 为归属资源（如
``InferenceService``）的 uid；分配到的设备记录在 ``allocation_devices`` 表
（``node_id`` / ``device_id``），设备行的状态由触发器
``trg_allocation_devices_follow_status`` 与所属分配同步。数据库层兜底的约束：

* 部分唯一索引 ``idx_allocations_one_active_per_tenant_resource``：同一租户的同一
  资源至多一个 Active 分配（取代不分租户的旧索引
  ``idx_allocations_one_active_per_resource``，打开存量库时删除旧索引）；
* 部分唯一索引 ``idx_allocation_devices_one_active``：同一设备至多属于一个 Active
  分配；
* ``allocations.idempotency_key`` 唯一：一个幂等键只对应一个分配，释放后也不复用。

:meth:`SQLiteRegistry.create_allocation` 在同一个 ``BEGIN IMMEDIATE`` 事务中写入
分配行与设备行；``release_allocation`` 走 ``ALLOCATION_STATE_MACHINE`` 校验。归属资源已不
存在的 Active 分配由 :meth:`SQLiteRegistry.list_orphaned_allocations` 列出、
:meth:`SQLiteRegistry.expire_orphaned_allocation` 标记为 ``Expired``（控制器回收孤立的分配）；
没有组件按 ``lease_until`` 让分配过期。分配的写入（含回收孤立的分配）不修改 ``resources``，
既不追加变更记录，也不写资源变更事件。

分页
----
:meth:`SQLiteRegistry.list_page` 按 ``(namespace, name)`` 升序（``namespace`` 为空视为
``""``）返回一页资源与下一页的不透明游标 ``next_page_token``，格式为 ``<载荷>.<签名>``：
载荷是 ``{"v": 1, "ns": ..., "name": ...}``（本页最后一个资源的位置）的 base64url 编码
（无填充），签名是以 ``registry_meta`` 表中持久化的随机密钥对「载荷 + 租户、kind 与过滤
条件」计算的 HMAC-SHA256（同样 base64url 编码）。游标因此绑定租户、kind 与过滤条件
（``limit`` 每页可以不同），被篡改或用于另一租户 / kind / 另一组过滤条件时抛
:class:`InvalidPageTokenError`；密钥随数据库保存，控制平面重启后游标仍然有效。游标记录
位置而不是快照：翻页期间新建或删除的资源可能出现也可能缺席，全程存在的资源恰好出现一次。
签发与校验见 :mod:`forgemesh.controlplane.pagetokens`，审计记录列表的游标使用同一个密钥。

变更记录
--------
每次写入 ``resources`` 都在**同一事务**中向 ``resource_changes`` 追加一条
记录（:class:`ChangeType`），控制器按 ``seq`` 增量轮询以发现待协调的资源
（不依赖 PostgreSQL ``LISTEN/NOTIFY``，见 ADR-013），处理完后调用
:meth:`SQLiteRegistry.prune_changes` 清理。

同一事务中还向 :attr:`SQLiteRegistry.outbox` 写入一条资源变更事件（ADR-008 事务性
Outbox，信封与事件类型见 :mod:`forgemesh.controlplane.events`）；API 写入（``put`` /
``update_spec`` / ``delete`` / ``request_delete`` 给出调用主体 ``principal``）再向
:attr:`SQLiteRegistry.audit_log` 写入一条审计记录。资源写入失败时不会留下事件与审计
记录，二者写入失败时资源写入一并回滚。

租户隔离
--------
面向租户的查询均以 ``tenant_id`` 作为首要过滤条件；跨租户访问一律视为
:class:`~forgemesh.core.errors.NotFoundError`，不泄露存在性。
:meth:`SQLiteRegistry.list_resource_keys` 与 :meth:`SQLiteRegistry.changes_since`
是例外：它们跨租户返回资源键，仅供控制平面内部的控制器使用，不得暴露给 API；
:meth:`SQLiteRegistry.get_allocation` 与 :meth:`SQLiteRegistry.allocated_devices`
同样跨租户读取，仅供调度器使用（设备由全部租户共享）。
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
import logging
import sqlite3
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar
from uuid import uuid4

from forgemesh.core.errors import ConflictError, NotFoundError, PreconditionFailedError
from forgemesh.core.objects import Condition, Resource
from forgemesh.core.registry import ResourceRegistry
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.allocation import Allocation, AllocationStatus
from forgemesh.resource.inference import Endpoint, InferenceService
from forgemesh.resource.states import ALLOCATION_STATE_MACHINE, INFERENCE_STATE_MACHINE
from pydantic import ValidationError

from ..audit import AuditEvent, AuditLog
from ..auth import Principal
from ..events import ResourceEventChange, resource_event
from ..outbox import Outbox
from ..pagetokens import InvalidPageTokenError, PageTokenSigner, load_page_token_key, page_scope
from ..sqlite import Database

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
#: 先受控释放 GPU/实例再物理删除。其余 phase（Pending/Scheduling/Failed/Unknown）
#: 的资源只有持有 Active 分配时才同样受控删除，否则直接物理删除。
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_allocations_one_active_per_tenant_resource
    ON allocations(tenant_id, resource_id) WHERE status = 'Active';
CREATE TABLE IF NOT EXISTS allocation_devices (
    allocation_id TEXT NOT NULL REFERENCES allocations(id),
    node_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (allocation_id, node_id, device_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_allocation_devices_one_active
    ON allocation_devices(node_id, device_id) WHERE status = 'Active';
CREATE TRIGGER IF NOT EXISTS trg_allocation_devices_follow_status
    AFTER UPDATE OF status ON allocations
BEGIN
    UPDATE allocation_devices SET status = NEW.status WHERE allocation_id = NEW.id;
END;
CREATE TABLE IF NOT EXISTS resource_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    namespace TEXT,
    name TEXT NOT NULL,
    change TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

#: 资源列表的游标位置：本页最后一个资源的 namespace 排序键与 name。
_PAGE_POSITION = {"ns": str, "name": str}


class ChangeType(StrEnum):
    """``resource_changes.change`` 的取值。"""

    CREATED = "created"
    UPDATED = "updated"
    STATUS = "status"
    TERMINATING = "terminating"
    DELETED = "deleted"


#: 变更记录类型 → 资源变更事件的变更类型。
_EVENT_CHANGES = {
    ChangeType.CREATED: ResourceEventChange.CREATED,
    ChangeType.UPDATED: ResourceEventChange.UPDATED,
    ChangeType.STATUS: ResourceEventChange.STATUS_UPDATED,
    ChangeType.TERMINATING: ResourceEventChange.TERMINATING,
    ChangeType.DELETED: ResourceEventChange.DELETED,
}

#: 变更记录类型 → 审计记录的 ``action``（受理的删除请求与物理删除都是 ``delete``）。status
#: 写入不带调用主体，不产生审计记录。取值须与 OpenAPI ``AuditEvent.action`` 的枚举一致。
_AUDIT_ACTIONS = {
    ChangeType.CREATED: "create",
    ChangeType.UPDATED: "update",
    ChangeType.TERMINATING: "delete",
    ChangeType.DELETED: "delete",
}


class ConflictReason(StrEnum):
    """:meth:`SQLiteRegistry.update_spec` 拒绝写入的原因（``ConflictError.details["reason"]``）。"""

    RESOURCE_VERSION = "ResourceVersionMismatch"
    UID = "UidMismatch"
    TERMINATING = "Terminating"


@dataclass(frozen=True)
class ResourceKey:
    """跨租户唯一定位一个资源。"""

    tenant_id: str
    kind: str
    namespace: str | None
    name: str


@dataclass(frozen=True)
class ResourceChange:
    """``resource_changes`` 中的一条记录。

    Attributes:
        seq: 单调递增的序号；``AUTOINCREMENT`` 保证清理后也不复用，否则消费方
            的游标会漏读清理之后写入的记录。
        key: 发生变更的资源。
        change: 变更类型。
    """

    seq: int
    key: ResourceKey
    change: ChangeType


@dataclass(frozen=True)
class ResourcePage:
    """:meth:`SQLiteRegistry.list_page` 返回的一页资源。

    Attributes:
        items: 本页资源，按 ``(namespace, name)`` 升序（``namespace`` 为空视为 ``""``）。
        next_page_token: 继续列出下一页的游标；没有更多资源时为 ``None``。
    """

    items: builtins.list[Resource]
    next_page_token: str | None = None


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


def _merge_conditions(
    existing: builtins.list[dict[str, Any]],
    updates: builtins.list[Condition],
    now: datetime,
) -> builtins.list[dict[str, Any]]:
    """按 ``type`` 合并条件；``lastTransitionTime`` 只在条件的 ``status`` 变化时刷新。"""
    merged = [dict(item) for item in existing]
    for update in updates:
        index = next(
            (i for i, item in enumerate(merged) if item.get("type") == update.type),
            None,
        )
        if index is not None and merged[index].get("status") == update.status:
            entry = update.model_dump(mode="json", exclude={"lastTransitionTime"})
            entry["lastTransitionTime"] = merged[index].get("lastTransitionTime")
        else:
            entry = update.model_copy(update={"lastTransitionTime": now}).model_dump(mode="json")
        if index is None:
            merged.append(entry)
        else:
            merged[index] = entry
    return merged


class SQLiteRegistry(ResourceRegistry):
    """基于 SQLite 的资源注册表。

    Args:
        db_path: SQLite 数据库文件路径，默认 ``:memory:``（内存库，单测友好）。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db = Database(db_path)
        self._conn = self._db.conn
        self._lock = self._db.lock
        self._init_schema()
        self._page_tokens = PageTokenSigner(load_page_token_key(self._conn))
        self._outbox = Outbox(database=self._db)
        self._audit = AuditLog(database=self._db)

    @property
    def outbox(self) -> Outbox:
        """资源变更事件所在的 Outbox（与注册表共用连接），供事件分发器发布。"""
        return self._outbox

    @property
    def audit_log(self) -> AuditLog:
        """API 写入的审计记录（与注册表共用连接）。"""
        return self._audit

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _run(self, fn: Callable[[], _T]) -> _T:
        """在工作线程中持锁执行一段同步数据库操作（读写均串行化）；``fn`` 抛出异常时
        回滚它未提交的写入，不会被下一次提交连带提交。"""
        return await self._db.run(fn)

    @staticmethod
    def _record_change(
        cur: sqlite3.Cursor,
        tenant_id: str,
        kind: str,
        namespace: str | None,
        name: str,
        change: ChangeType,
    ) -> None:
        """在调用方的事务中追加一条变更记录（随调用方一起提交或回滚）。"""
        cur.execute(
            "INSERT INTO resource_changes (tenant_id, kind, namespace, name, change) "
            "VALUES (?, ?, ?, ?, ?)",
            (tenant_id, kind, namespace, name, change.value),
        )

    def _emit(
        self,
        key: ResourceKey,
        change: ChangeType,
        body: str,
        version: int,
        phase: str | None,
        correlation_id: str | None,
        principal: Principal | None = None,
    ) -> None:
        """在调用方的事务中写入一条资源变更事件；给出 ``principal``（API 写入）时再写入一条
        审计记录。二者随调用方一起提交或回滚，使用同一个关联 ID（为空时生成一个新的）。

        ``body`` / ``version`` / ``phase`` 描述写入之后的资源；删除时为删除前最后的状态。
        """
        correlation_id = correlation_id or uuid4().hex
        resource = self._body_to_resource(body)
        event = resource_event(
            tenant_id=key.tenant_id,
            kind=key.kind,
            namespace=key.namespace,
            name=key.name,
            change=_EVENT_CHANGES[change],
            resource=resource.model_dump(mode="json", by_alias=True),
            version=version,
            phase=phase,
            uid=resource.metadata.uid,
            correlation_id=correlation_id,
        )
        self._outbox.put(event)
        if principal is None:
            return
        self._audit.put(
            AuditEvent(
                correlation_id=correlation_id,
                tenant_id=key.tenant_id,
                actor=principal.subject,
                key_id=principal.key_id,
                action=_AUDIT_ACTIONS[change],
                resource_type=key.kind,
                namespace=key.namespace,
                resource_id=key.name,
                outcome="success",
                details={
                    "change": _EVENT_CHANGES[change].value,
                    "resourceUid": resource.metadata.uid,
                    "resourceVersion": version,
                    "eventId": event.event_id,
                },
                timestamp=event.created_at,
            )
        )

    @staticmethod
    def _check_principal(principal: Principal | None, tenant_id: str) -> None:
        """审计记录的主体须属于被写入资源的租户。"""
        if principal is not None and principal.tenant_id != tenant_id:
            raise ValueError(
                f"principal of tenant {principal.tenant_id!r} cannot write resources of "
                f"tenant {tenant_id!r}"
            )

    @contextlib.contextmanager
    def _immediate_transaction(self) -> Iterator[sqlite3.Cursor]:
        """``BEGIN IMMEDIATE`` 事务：开始即持有写锁，事务内的检查不会被其他连接的
        写入穿插；异常时回滚，正常退出时提交。"""
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    def _allocation_record(self, row: sqlite3.Row) -> dict[str, Any]:
        """分配行 → 记录字典：各列加 ``devices``（按分配时的顺序）。"""
        record = dict(row)
        record["devices"] = [
            {"node_id": device["node_id"], "device_id": device["device_id"]}
            for device in self._conn.execute(
                "SELECT node_id, device_id FROM allocation_devices "
                "WHERE allocation_id=? ORDER BY rowid",
                (row["id"],),
            )
        ]
        return record

    def _init_schema(self) -> None:
        """创建表与索引（幂等），并对存量库做列与索引迁移。"""
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate_legacy_resources()
        self._migrate_legacy_allocations()
        self._ensure_identity_index()
        self._conn.commit()

    def _migrate_legacy_allocations(self) -> None:
        """删除存量库中不分租户的旧索引 ``idx_allocations_one_active_per_resource``。

        取代它的 ``idx_allocations_one_active_per_tenant_resource`` 只在同一租户内约束
        ``resource_id``，旧索引成立的数据必然满足新索引；存量分配记录原样保留。
        """
        legacy = self._conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='index' AND name='idx_allocations_one_active_per_resource'"
        ).fetchone()
        if legacy is not None:
            self._conn.execute("DROP INDEX idx_allocations_one_active_per_resource")
            logger.info(
                "migrated legacy allocations table: replaced index "
                "'idx_allocations_one_active_per_resource' with "
                "'idx_allocations_one_active_per_tenant_resource'"
            )

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
    def _holds_allocation(cur: sqlite3.Cursor, tenant_id: str, uid: str | None) -> bool:
        """uid 为 ``uid`` 的资源当前是否持有 Active 分配（没有 uid 的资源不会持有）。"""
        if uid is None:
            return False
        row = cur.execute(
            "SELECT 1 FROM allocations WHERE tenant_id=? AND resource_id=? AND status=?",
            (tenant_id, uid, AllocationStatus.ACTIVE.value),
        ).fetchone()
        return row is not None

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
    def _page_scope(
        tenant_id: str, kind: str, namespace: str | None, labels: dict[str, str] | None
    ) -> str:
        """分页游标绑定的上下文：租户、kind 与过滤条件（不含 ``limit``）。"""
        return page_scope(
            {
                "tenant": tenant_id,
                "kind": kind,
                "namespace": namespace,
                "labels": sorted((labels or {}).items()),
            }
        )

    def _encode_page_token(self, scope: str, namespace_key: str, name: str) -> str:
        """把本页最后一个资源的位置编码为签名游标。"""
        return self._page_tokens.encode({"ns": namespace_key, "name": name}, scope)

    def _decode_page_token(self, token: str, scope: str) -> tuple[str, str]:
        """校验签名并解码游标，返回 ``(namespace 排序键, name)``。

        Raises:
            InvalidPageTokenError: 游标无法解析、被篡改，或属于另一个上下文 ``scope``。
        """
        namespace_key, name = self._page_tokens.decode(token, scope, _PAGE_POSITION)
        return namespace_key, name

    @staticmethod
    def _row_to_resource(row: sqlite3.Row) -> Resource:
        """将数据库行反序列化为 :class:`Resource`。

        受管 kind（登记于 :data:`_KIND_MODELS`）优先按强类型子类解析（保留
        ``spec`` / ``status``）；解析失败（如基类形状的 payload——部分调用方
        与既有测试直接以基类 ``Resource`` 写入）或 kind 未登记时，回退基类并
        投影到基类已知字段（丢弃未知扩展字段），保证旧 payload / 第三方 kind
        的读回兼容。
        """
        return SQLiteRegistry._body_to_resource(row["body"])

    @staticmethod
    def _body_to_resource(body: str) -> Resource:
        """按 :meth:`_row_to_resource` 的规则反序列化 payload。"""
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
        *,
        principal: Principal | None = None,
    ) -> Resource:
        """写入（Create 或 Update-CAS）资源，返回写入后的最新对象。

        * ``expected_version`` 为 ``None`` → Create；同名资源已存在抛 ConflictError。
        * ``expected_version`` 非 ``None`` → Update-CAS；版本不匹配或资源不存在抛
          ConflictError。成功后 ``resource_version`` 递增。

        同一事务中写入资源变更事件（``correlation_id`` 为空时生成一个新的）；给出
        ``principal``（API 写入的调用主体）时再写入一条审计记录。``actor`` 不入库。

        Raises:
            ValueError: ``principal`` 不属于资源的租户。
        """
        meta = resource.metadata
        tenant_id = meta.tenant_id
        if tenant_id is None:
            raise ConflictError("metadata.tenant_id (tenantId) must not be empty")
        self._check_principal(principal, tenant_id)
        namespace = meta.namespace
        name = meta.name
        kind = resource.kind
        key = ResourceKey(tenant_id, kind, namespace, name)
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
                    self._record_change(cur, tenant_id, kind, namespace, name, ChangeType.CREATED)
                    self._emit(
                        key,
                        ChangeType.CREATED,
                        body,
                        new_version,
                        phase,
                        correlation_id,
                        principal,
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
                self._record_change(cur, tenant_id, kind, namespace, name, ChangeType.UPDATED)
                self._emit(
                    key, ChangeType.UPDATED, body, new_version, phase, correlation_id, principal
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
        """按 ``kind`` + 可选 ``namespace`` / ``labels`` 过滤列出一页资源。

        与 :meth:`list_page` 相同，只是不返回下一页游标；``continue_token`` 必须是
        :meth:`list_page` 以相同租户、kind 与过滤条件返回的 ``next_page_token``。
        ``watch=True`` 在阶段 P1 暂未实现，抛 :class:`NotImplementedError`。

        Raises:
            ValueError: ``limit`` 小于 1，或标签键包含双引号 / 反斜杠。
            InvalidPageTokenError: ``continue_token`` 无效。
        """
        if watch:
            raise NotImplementedError("watch stream is not implemented in phase P1")
        page = await self.list_page(
            kind, tenant_id, namespace, labels, page_token=continue_token, limit=limit
        )
        return page.items

    async def list_page(
        self,
        kind: str,
        tenant_id: str,
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
        page_token: str | None = None,
        limit: int = 100,
    ) -> ResourcePage:
        """按 ``kind`` + 可选 ``namespace`` / ``labels`` 过滤列出一页资源与下一页游标。

        结果按 ``(namespace, name)`` 升序（``namespace`` 为空视为 ``""``），标签过滤在 SQL
        中先于 ``LIMIT`` 执行，保证每页返回的都是匹配项。还有更多匹配项时
        ``next_page_token`` 非空：以相同的租户、kind 与过滤条件把它作为 ``page_token``
        传回即得下一页（``limit`` 可以不同），游标格式见模块文档「分页」。

        Raises:
            ValueError: ``limit`` 小于 1，或标签键包含双引号 / 反斜杠。
            InvalidPageTokenError: ``page_token`` 无法解析、被篡改，或签发给了另一个租户、
                kind 或另一组过滤条件。
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        label_paths = [
            (self._label_path(key), value) for key, value in sorted((labels or {}).items())
        ]
        scope = self._page_scope(tenant_id, kind, namespace, labels)
        after = self._decode_page_token(page_token, scope) if page_token is not None else None

        def _list_page() -> ResourcePage:
            sql = (
                "SELECT body, COALESCE(namespace, '') AS ns_key, name FROM resources "
                "WHERE tenant_id=? AND kind=?"
            )
            params: list[Any] = [tenant_id, kind]
            if namespace is not None:
                sql += " AND namespace IS ?"
                params.append(namespace)
            for path, value in label_paths:
                sql += " AND json_extract(body, ?) = ?"
                params.extend([path, value])
            if after is not None:
                after_ns_key, after_name = after
                sql += (
                    " AND (COALESCE(namespace, '') > ?"
                    " OR (COALESCE(namespace, '') = ? AND name > ?))"
                )
                params.extend([after_ns_key, after_ns_key, after_name])
            # 多取一行，用来判断是否还有下一页
            sql += " ORDER BY COALESCE(namespace, ''), name LIMIT ?"
            params.append(limit + 1)
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            items = [self._row_to_resource(row) for row in rows[:limit]]
            if len(rows) <= limit:
                return ResourcePage(items)
            last = rows[limit - 1]
            return ResourcePage(items, self._encode_page_token(scope, last["ns_key"], last["name"]))

        return await self._run(_list_page)

    async def delete(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
        actor: str | None = None,
        correlation_id: str | None = None,
        *,
        principal: Principal | None = None,
    ) -> None:
        """删除资源；不存在或跨租户时抛 :class:`NotFoundError`。

        同一事务中写入资源变更事件与（给出 ``principal`` 时）审计记录，同 :meth:`put`。
        """
        self._check_principal(principal, tenant_id)
        key = ResourceKey(tenant_id, kind, namespace, name)

        def _delete() -> None:
            cur = self._conn.cursor()
            # 先持有写锁再读取删除前的状态（事件载荷），读取与删除之间不会穿插其他连接的写入。
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT body, phase, resource_version FROM resources "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                (tenant_id, kind, namespace, name),
            ).fetchone()
            if row is None:
                self._conn.rollback()
                raise NotFoundError(
                    f"resource not found: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            cur.execute(
                "DELETE FROM resources WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                (tenant_id, kind, namespace, name),
            )
            self._record_change(cur, tenant_id, kind, namespace, name, ChangeType.DELETED)
            self._emit(
                key,
                ChangeType.DELETED,
                row["body"],
                row["resource_version"],
                row["phase"],
                correlation_id,
                principal,
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
        *,
        principal: Principal | None = None,
    ) -> RequestDeleteResult:
        """受控删除入口：finalizer 非空 → ``Terminating``；为空 → 物理 DELETE。

        规则（对齐 architecture.md §5.3/§12.4 与 development-plan 第 7 章删除
        闭环；``phase`` 存于 ``resources.phase`` 元数据列，权威状态机校验）：

        * 资源已处于 ``Terminating`` → 幂等返回（不重复递增版本）；
        * finalizers 非空 → 持久化 ``phase='Terminating'``（非法转移抛
          :class:`~forgemesh.core.errors.InvalidStateTransition`），返回
          ``deleted=False``，由协调器驱动受控释放；
        * finalizers 为空，但 phase 属于有副作用态（:data:`SIDE_EFFECT_PHASES`，
          如 ``Ready``）或资源（按 uid）持有 Active 分配 → 自动追加默认 finalizer
          :data:`DEFAULT_RELEASE_FINALIZER` 后同上，分配由协调器在物理删除前释放；
        * 其余（无 finalizer、非副作用态且没有 Active 分配）直接物理 DELETE（与
          :meth:`delete` 行为一致），以读到的版本且「仍没有 Active 分配」为条件。

        payload 与 ``finalizers`` 列双写保持一致；子类 payload 的
        ``status.phase``（若存在）同步为 ``Terminating``，基类 payload 不引入
        ``status`` 子树。

        Args:
            kind: 资源类型标识。
            namespace: 命名空间（可为 ``None``）。
            name: 资源名。
            tenant_id: 租户标识（多租户边界）。
            actor: 操作发起者标识（只用于日志）。
            correlation_id: 关联追踪标识，写入同一事务中的资源变更事件与审计记录（为空时
                生成一个新的）。
            principal: API 写入的调用主体；给出时同一事务中写入一条审计记录。幂等返回
                （已处于 ``Terminating``）时不写入任何记录。

        Returns:
            :class:`RequestDeleteResult`：``deleted=True`` 已物理删除；
            ``deleted=False`` 已进入 ``Terminating``。

        Raises:
            NotFoundError: 目标资源不存在（或跨租户不可见）。
            InvalidStateTransition: 当前 phase 不允许转移到 ``Terminating``。
            ConflictError: 读取后资源被并发修改（``resource_version`` 已变化）或
                并发获得了分配，调用方应重新读取后重试。
            ValueError: ``principal`` 不属于资源的租户。
        """
        self._check_principal(principal, tenant_id)
        terminating = "Terminating"
        active = AllocationStatus.ACTIVE.value
        key = ResourceKey(tenant_id, kind, namespace, name)

        def _request_delete() -> RequestDeleteResult:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT body, phase, finalizers, resource_version, uid FROM resources "
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
            if not effective and (
                phase in SIDE_EFFECT_PHASES or self._holds_allocation(cur, tenant_id, row["uid"])
            ):
                effective.append(DEFAULT_RELEASE_FINALIZER)
            if not effective:
                # 无 finalizer、无外部副作用 → 直接物理删除。以读到的版本且仍没有
                # Active 分配为条件：并发写入可能刚追加了 finalizer 或分配。
                cur.execute(
                    "DELETE FROM resources "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=? "
                    " AND resource_version=? AND NOT EXISTS ("
                    "  SELECT 1 FROM allocations"
                    "  WHERE tenant_id=? AND resource_id=? AND status=?)",
                    (tenant_id, kind, namespace, name, read_version, tenant_id, row["uid"], active),
                )
                if cur.rowcount == 0:
                    self._conn.rollback()
                    raise ConflictError(
                        f"request_delete raced with concurrent write: kind={kind} "
                        f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                    )
                self._record_change(cur, tenant_id, kind, namespace, name, ChangeType.DELETED)
                self._emit(
                    key,
                    ChangeType.DELETED,
                    row["body"],
                    read_version,
                    phase,
                    correlation_id,
                    principal,
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
            new_body = json.dumps(body, ensure_ascii=False)
            cur.execute(
                "UPDATE resources SET body=?, phase=?, finalizers=?, "
                " resource_version=?, updated_at=datetime('now') "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=? "
                " AND resource_version=?",
                (
                    new_body,
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
            self._record_change(cur, tenant_id, kind, namespace, name, ChangeType.TERMINATING)
            self._emit(
                key,
                ChangeType.TERMINATING,
                new_body,
                new_version,
                terminating,
                correlation_id,
                principal,
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

    async def update_status(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
        status_patch: dict[str, Any],
        *,
        observed_generation: int | None = None,
        expected_version: str | None = None,
        correlation_id: str | None = None,
    ) -> Resource:
        """status 子资源写入：只修改状态条件、phase、实例地址与 observedGeneration。

        不触碰 spec 与其余元数据，不递增 ``generation``。规则：

        * ``status_patch["conditions"]`` 按 ``type`` 合并到已有条件：payload 带
          ``status`` 子树（强类型资源）时写入 ``status.conditions``，否则写入
          顶层 ``conditions``；``lastTransitionTime`` 由注册中心维护，只在条件的
          ``status`` 变化时刷新；
        * ``status_patch["phase"]`` 经权威状态机校验，只适用于带 ``status`` 子树
          的资源；
        * ``status_patch["endpoints"]`` 按 :class:`~forgemesh.resource.inference.Endpoint`
          校验后整体替换 ``status.endpoints``，只适用于带 ``status`` 子树的资源；
        * ``observed_generation`` 写入元数据列与 ``metadata.observedGeneration``，
          payload 已有 ``status.observedGeneration`` 时一并更新；
        * ``expected_version`` 非空时做 CAS，冲突语义与 :meth:`put` 相同；
        * 合并后没有任何变化时不写库，``resource_version`` 不变，也不追加变更记录与事件；
        * ``correlation_id`` 写入同一事务中的资源变更事件（为空时生成一个新的）。

        Returns:
            写入后的资源；没有变化时为当前资源。

        Raises:
            NotFoundError: 资源不存在（或跨租户不可见）。
            ConflictError: ``expected_version`` 与当前版本不一致。
            InvalidStateTransition: phase 转移非法。
            ValueError: 补丁含 ``conditions`` / ``phase`` / ``endpoints`` 以外的字段、
                非法条件或实例地址，或为没有 ``status`` 子树的资源设置 phase 或实例地址。
        """
        unknown = sorted(set(status_patch) - {"conditions", "phase", "endpoints"})
        if unknown:
            raise ValueError(f"unsupported status fields: {unknown}")
        updates = [Condition.model_validate(item) for item in status_patch.get("conditions", [])]
        raw_phase = status_patch.get("phase")
        target_phase = str(raw_phase) if raw_phase is not None else None
        raw_endpoints = status_patch.get("endpoints")
        endpoints = (
            [Endpoint.model_validate(item).model_dump(mode="json") for item in raw_endpoints]
            if raw_endpoints is not None
            else None
        )
        key = ResourceKey(tenant_id, kind, namespace, name)

        def _update_status() -> Resource:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT body, phase, observed_generation, resource_version FROM resources "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                (tenant_id, kind, namespace, name),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"resource not found: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            read_version = row["resource_version"]
            if expected_version is not None and int(expected_version) != read_version:
                raise ConflictError(
                    f"status update failed: expected_version={expected_version!r} "
                    f"does not match current {read_version} "
                    f"(kind={kind} namespace={namespace!r} name={name})",
                )
            original = json.loads(row["body"])
            body = json.loads(row["body"])
            status = body.get("status")
            has_status = isinstance(status, dict)
            phase = row["phase"]
            if target_phase is not None:
                if not has_status:
                    raise ValueError(f"{kind} payload has no status subtree to hold a phase")
                self._validate_phase_transition(kind, phase, target_phase)
                status["phase"] = target_phase
                phase = target_phase
            if endpoints is not None:
                if not has_status:
                    raise ValueError(f"{kind} payload has no status subtree to hold endpoints")
                status["endpoints"] = endpoints
            if updates:
                holder = status if has_status else body
                holder["conditions"] = _merge_conditions(
                    holder.get("conditions") or [], updates, datetime.now(UTC)
                )
            observed = row["observed_generation"]
            if observed_generation is not None:
                observed = observed_generation
                body["metadata"]["observedGeneration"] = observed_generation
                if has_status and "observedGeneration" in status:
                    status["observedGeneration"] = observed_generation
            if body == original:
                return self._body_to_resource(row["body"])

            new_version = read_version + 1
            body["metadata"]["resourceVersion"] = str(new_version)
            new_body = json.dumps(body, ensure_ascii=False)
            updated = self._body_to_resource(new_body)
            cur.execute(
                "UPDATE resources SET body=?, phase=?, observed_generation=?, "
                " resource_version=?, updated_at=datetime('now') "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=? "
                " AND resource_version=?",
                (
                    new_body,
                    phase,
                    observed,
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
                    f"status update raced with concurrent write: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            self._record_change(cur, tenant_id, kind, namespace, name, ChangeType.STATUS)
            self._emit(key, ChangeType.STATUS, new_body, new_version, phase, correlation_id)
            self._conn.commit()
            return updated

        return await self._run(_update_status)

    async def update_spec(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
        spec: dict[str, Any],
        *,
        labels: dict[str, str],
        annotations: dict[str, str],
        expected_version: str | None = None,
        expected_uid: str | None = None,
        correlation_id: str | None = None,
        principal: Principal | None = None,
    ) -> Resource:
        """期望状态写入：整体替换 ``spec``、``metadata.labels`` 与 ``metadata.annotations``。

        只适用于登记了强类型模型的 kind（:data:`_KIND_MODELS`），写入结果须满足该模型；
        status、条件、finalizer、uid 与 ``observedGeneration`` 原样保留。规则：

        * ``spec`` 按模型规范化（补齐默认值）后与当前值比较，变化时 ``generation`` 加 1；
          只有 labels / annotations 变化时 ``generation`` 不变；
        * 有变化时 ``resource_version`` 加 1，并追加 ``updated`` 变更记录（控制器据此
          重新协调）与资源变更事件（``correlation_id`` 为空时生成一个新的），给出
          ``principal``（API 写入的调用主体）时再写入一条审计记录；没有任何变化时不写库，
          返回当前资源；
        * 资源处于 ``Terminating`` 时拒绝，先于 uid / 版本检查（重新读取后重试也不会成功）；
        * ``expected_uid`` / ``expected_version`` 非空时须与当前 uid / 版本相同（版本按
          字符串比较）；同名资源被删除后重建时版本会从 0 重新计数，uid 可以识别这种情况。

        检查与写入在同一个 ``BEGIN IMMEDIATE`` 事务中完成。

        Returns:
            写入后的资源；没有变化时为当前资源。

        Raises:
            NotFoundError: 资源不存在（或跨租户不可见）。
            ConflictError: 资源处于 ``Terminating``，或 uid / 版本与预期不同；
                ``details["reason"]`` 为 :class:`ConflictReason` 的取值。
            ValueError: kind 没有登记强类型模型，或 ``principal`` 不属于资源的租户。
            pydantic.ValidationError: 替换后的资源不满足模型约束（``ValueError`` 的子类）。
        """
        model = _KIND_MODELS.get(kind)
        if model is None:
            raise ValueError(f"spec updates are not supported for kind {kind!r}")
        self._check_principal(principal, tenant_id)
        target = f"kind={kind} namespace={namespace!r} name={name}"
        key = ResourceKey(tenant_id, kind, namespace, name)

        def _update_spec() -> Resource:
            with self._immediate_transaction() as cur:
                row = cur.execute(
                    "SELECT body, phase, uid, generation, resource_version FROM resources "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                    (tenant_id, kind, namespace, name),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"resource not found: {target} tenant_id={tenant_id}")
                if row["phase"] == "Terminating":
                    raise ConflictError(
                        f"resource is terminating and no longer accepts updates: {target}",
                        details={"reason": ConflictReason.TERMINATING.value},
                    )
                if expected_uid is not None and expected_uid != row["uid"]:
                    raise ConflictError(
                        f"uid {expected_uid!r} does not match the current resource: {target}",
                        details={"reason": ConflictReason.UID.value},
                    )
                read_version = row["resource_version"]
                if expected_version is not None and expected_version != str(read_version):
                    raise ConflictError(
                        f"resourceVersion {expected_version!r} does not match the current "
                        f"version {read_version}: {target}",
                        details={"reason": ConflictReason.RESOURCE_VERSION.value},
                    )
                body = json.loads(row["body"])
                metadata = body["metadata"]
                replaced = model.model_validate(
                    {
                        **body,
                        "spec": spec,
                        "metadata": {**metadata, "labels": labels, "annotations": annotations},
                    }
                )
                new_spec = replaced.model_dump(mode="json", by_alias=True)["spec"]
                new_labels = dict(replaced.metadata.labels)
                new_annotations = dict(replaced.metadata.annotations)
                spec_changed = new_spec != self._normalised_spec(model, body)
                if (
                    not spec_changed
                    and new_labels == metadata.get("labels", {})
                    and new_annotations == metadata.get("annotations", {})
                ):
                    return self._body_to_resource(row["body"])

                generation = row["generation"] + 1 if spec_changed else row["generation"]
                new_version = read_version + 1
                body["spec"] = new_spec
                metadata["labels"] = new_labels
                metadata["annotations"] = new_annotations
                metadata["generation"] = generation
                metadata["resourceVersion"] = str(new_version)
                new_body = json.dumps(body, ensure_ascii=False)
                cur.execute(
                    "UPDATE resources SET body=?, generation=?, resource_version=?, "
                    " updated_at=datetime('now') "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                    (new_body, generation, new_version, tenant_id, kind, namespace, name),
                )
                self._record_change(cur, tenant_id, kind, namespace, name, ChangeType.UPDATED)
                self._emit(
                    key,
                    ChangeType.UPDATED,
                    new_body,
                    new_version,
                    row["phase"],
                    correlation_id,
                    principal,
                )
                return self._body_to_resource(new_body)

        return await self._run(_update_spec)

    @staticmethod
    def _normalised_spec(model: type[Resource], body: dict[str, Any]) -> Any:
        """payload 中 ``spec`` 按模型规范化后的 JSON；payload 不满足模型时取原值。"""
        try:
            return model.model_validate(body).model_dump(mode="json", by_alias=True).get("spec")
        except ValidationError:
            return body.get("spec")

    # ------------------------------------------------------------------ #
    # 控制器读取（跨租户，仅供控制平面内部使用）
    # ------------------------------------------------------------------ #

    async def get_with_phase(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
    ) -> tuple[Resource, str | None]:
        """读取资源及 ``phase`` 列记录的生命周期阶段。

        基类资源的 payload 没有 ``status`` 子树，``Terminating`` 只记录在 ``phase``
        列（见 :meth:`request_delete`），只读 payload 无法判断是否进入受控删除。

        Raises:
            NotFoundError: 资源不存在（或跨租户不可见）。
        """

        def _get_with_phase() -> tuple[Resource, str | None]:
            row = self._conn.execute(
                "SELECT body, phase FROM resources "
                "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                (tenant_id, kind, namespace, name),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"resource not found: kind={kind} "
                    f"namespace={namespace!r} name={name} tenant_id={tenant_id}",
                )
            return self._row_to_resource(row), row["phase"]

        return await self._run(_get_with_phase)

    async def list_resource_keys(
        self, kinds: Collection[str] | None = None
    ) -> builtins.list[ResourceKey]:
        """跨租户列出资源键，供控制器在启动和全量同步时重建工作队列。

        Args:
            kinds: 只列出这些 kind；``None`` 表示全部。
        """
        if kinds is not None and not kinds:
            return []
        kind_list = sorted(kinds) if kinds is not None else []

        def _list_keys() -> list[ResourceKey]:
            sql = "SELECT tenant_id, kind, namespace, name FROM resources"
            if kind_list:
                sql += f" WHERE kind IN ({', '.join('?' for _ in kind_list)})"
            sql += " ORDER BY tenant_id, kind, COALESCE(namespace, ''), name"
            rows = self._conn.execute(sql, tuple(kind_list)).fetchall()
            return [
                ResourceKey(row["tenant_id"], row["kind"], row["namespace"], row["name"])
                for row in rows
            ]

        return await self._run(_list_keys)

    async def changes_since(self, seq: int, limit: int = 500) -> builtins.list[ResourceChange]:
        """按序号升序返回 ``seq`` 之后的至多 ``limit`` 条变更记录。

        Raises:
            ValueError: ``limit`` 小于 1。
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        def _changes() -> list[ResourceChange]:
            rows = self._conn.execute(
                "SELECT seq, tenant_id, kind, namespace, name, change FROM resource_changes "
                "WHERE seq > ? ORDER BY seq LIMIT ?",
                (seq, limit),
            ).fetchall()
            return [
                ResourceChange(
                    seq=row["seq"],
                    key=ResourceKey(row["tenant_id"], row["kind"], row["namespace"], row["name"]),
                    change=ChangeType(row["change"]),
                )
                for row in rows
            ]

        return await self._run(_changes)

    async def latest_change_seq(self) -> int:
        """当前最大的变更序号；没有记录时返回 0。"""

        def _latest() -> int:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM resource_changes"
            ).fetchone()
            return int(row["seq"])

        return await self._run(_latest)

    async def prune_changes(self, up_to_seq: int) -> int:
        """删除序号不大于 ``up_to_seq`` 的变更记录，返回删除条数。"""

        def _prune() -> int:
            cur = self._conn.execute("DELETE FROM resource_changes WHERE seq <= ?", (up_to_seq,))
            self._conn.commit()
            return cur.rowcount

        return await self._run(_prune)

    # ------------------------------------------------------------------ #
    # Allocation 持久化（development-plan 7.2 DDL）
    # ------------------------------------------------------------------ #

    async def put_allocation(self, allocation: Allocation) -> str:
        """插入一条不持有设备的分配记录，返回新生成的分配 id。

        不校验归属资源是否存在；调度器经 :meth:`create_allocation` 写入分配。
        同一租户的同一 ``resource_id`` 已存在 ``Active`` 分配时，由部分唯一索引
        ``idx_allocations_one_active_per_tenant_resource`` 兜底拒绝；重复
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

    async def create_allocation(
        self,
        tenant_id: str,
        resource: ResourceRef,
        idempotency_key: str,
        devices: Sequence[AllocatedDevice] = (),
    ) -> dict[str, Any]:
        """为 ``resource`` 创建持有 ``devices`` 的 Active 分配，返回分配记录。

        检查与写入在同一个 ``BEGIN IMMEDIATE`` 事务中完成（另一连接的写入无法穿插），
        分配行与设备行一起提交或一起回滚：

        * ``idempotency_key`` 已属于该资源（同租户、同 uid）的 Active 分配 → 原样返回
          该分配，不写库，忽略 ``devices``；
        * ``idempotency_key`` 已被其他分配使用（含已释放 / 过期的分配）→ 冲突；
        * 归属资源须存在、uid 一致且不处于 ``Terminating``；
        * 该资源已有 Active 分配，或任一设备已属于某个 Active 分配 → 冲突，不写库。

        Args:
            tenant_id: 租户标识（多租户边界）。
            resource: 归属资源；分配以其 ``uid`` 记为 ``resource_id``。
            idempotency_key: 分配的幂等键。
            devices: 分配的设备（可为空：没有加速器需求的资源）。

        Returns:
            分配记录：``allocations`` 各列加 ``devices``（``node_id`` / ``device_id``）。

        Raises:
            NotFoundError: 归属资源不存在，或同名资源的 uid 不同（原资源已被删除）。
            ConflictError: 见上；以及归属资源处于 ``Terminating``。
            ValueError: ``devices`` 含重复设备。
        """
        if len(set(devices)) != len(devices):
            raise ValueError(f"duplicate devices in allocation request: {list(devices)}")
        active = AllocationStatus.ACTIVE.value

        def _create() -> dict[str, Any]:
            with self._immediate_transaction() as cur:
                existing = cur.execute(
                    "SELECT * FROM allocations WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    owner = (existing["tenant_id"], existing["resource_id"], existing["status"])
                    if owner == (tenant_id, resource.uid, active):
                        return self._allocation_record(existing)
                    raise ConflictError(
                        f"allocation idempotency_key already used: {idempotency_key!r}"
                    )
                row = cur.execute(
                    "SELECT uid, phase FROM resources "
                    "WHERE tenant_id=? AND kind=? AND namespace IS ? AND name=?",
                    (tenant_id, resource.kind, resource.namespace, resource.name),
                ).fetchone()
                if row is None or row["uid"] != resource.uid:
                    raise NotFoundError(
                        f"resource not found: kind={resource.kind} "
                        f"namespace={resource.namespace!r} name={resource.name} "
                        f"uid={resource.uid} tenant_id={tenant_id}",
                    )
                if row["phase"] == "Terminating":
                    raise ConflictError(
                        f"resource is terminating: kind={resource.kind} "
                        f"namespace={resource.namespace!r} name={resource.name}",
                    )
                held = cur.execute(
                    "SELECT id FROM allocations WHERE tenant_id=? AND resource_id=? AND status=?",
                    (tenant_id, resource.uid, active),
                ).fetchone()
                if held is not None:
                    raise ConflictError(
                        "resource already has an active allocation: "
                        f"resource_id={resource.uid!r} allocation_id={held['id']}",
                    )
                for device in devices:
                    taken = cur.execute(
                        "SELECT 1 FROM allocation_devices "
                        "WHERE node_id=? AND device_id=? AND status=?",
                        (device.node_id, device.device_id, active),
                    ).fetchone()
                    if taken is not None:
                        raise ConflictError(
                            f"device already allocated: node_id={device.node_id} "
                            f"device_id={device.device_id}",
                        )
                allocation_id = uuid4().hex
                cur.execute(
                    "INSERT INTO allocations (id, tenant_id, resource_id, status, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (allocation_id, tenant_id, resource.uid, active, idempotency_key),
                )
                cur.executemany(
                    "INSERT INTO allocation_devices (allocation_id, node_id, device_id, status) "
                    "VALUES (?, ?, ?, ?)",
                    [(allocation_id, d.node_id, d.device_id, active) for d in devices],
                )
                created = cur.execute(
                    "SELECT * FROM allocations WHERE id=?", (allocation_id,)
                ).fetchone()
                return self._allocation_record(created)

        try:
            return await self._run(_create)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"allocation rejected by a database constraint: {exc}") from exc

    async def get_allocation(self, allocation_id: str) -> dict[str, Any] | None:
        """按 id 读取分配记录（跨租户，仅供调度器释放分配）；不存在返回 ``None``。"""

        def _get() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM allocations WHERE id=?", (allocation_id,)
            ).fetchone()
            return self._allocation_record(row) if row is not None else None

        return await self._run(_get)

    async def get_allocation_by_idempotency_key(
        self,
        idempotency_key: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        """读取以 ``idempotency_key`` 创建的分配（含终态）；本租户没有时返回 ``None``。"""

        def _get_by_key() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM allocations WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            return self._allocation_record(row) if row is not None else None

        return await self._run(_get_by_key)

    async def allocated_devices(self) -> frozenset[AllocatedDevice]:
        """属于 Active 分配的全部设备（跨租户：设备由全部租户共享）。"""

        def _allocated() -> frozenset[AllocatedDevice]:
            rows = self._conn.execute(
                "SELECT node_id, device_id FROM allocation_devices WHERE status=?",
                (AllocationStatus.ACTIVE.value,),
            ).fetchall()
            return frozenset(
                AllocatedDevice(node_id=row["node_id"], device_id=row["device_id"]) for row in rows
            )

        return await self._run(_allocated)

    async def get_active_allocation(
        self,
        resource_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        """读取 ``resource_id``（归属资源 uid）当前唯一的 Active 分配；不存在返回 ``None``。

        强制以 ``tenant_id`` 过滤（与 resources 一致的多租户边界）。记录含 ``devices``。
        """

        def _get_active() -> dict[str, Any] | None:
            row = self._conn.execute(
                "SELECT * FROM allocations WHERE tenant_id=? AND resource_id=? AND status=?",
                (tenant_id, resource_id, AllocationStatus.ACTIVE.value),
            ).fetchone()
            return self._allocation_record(row) if row is not None else None

        return await self._run(_get_active)

    async def release_active_allocation(
        self,
        resource_id: str,
        tenant_id: str,
        reason: str | None = None,
    ) -> str | None:
        """释放 ``resource_id``（归属资源 uid）当前的 Active 分配及其设备。

        没有 Active 分配（含已被并发释放或过期）时返回 ``None``，可重复调用。

        Args:
            resource_id: 归属资源 uid。
            tenant_id: 租户标识（多租户边界，强制过滤）。
            reason: 释放原因（仅记录日志）。

        Returns:
            被释放的分配 id；没有可释放的分配时为 ``None``。
        """
        active = AllocationStatus.ACTIVE.value

        def _release_active() -> str | None:
            row = self._conn.execute(
                "SELECT id FROM allocations WHERE tenant_id=? AND resource_id=? AND status=?",
                (tenant_id, resource_id, active),
            ).fetchone()
            if row is None:
                return None
            cur = self._conn.execute(
                "UPDATE allocations SET status=?, released_at=datetime('now') "
                "WHERE id=? AND status=?",
                (AllocationStatus.RELEASED.value, row["id"], active),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            logger.debug(
                "allocation released: id=%s tenant_id=%s resource_id=%s reason=%s",
                row["id"],
                tenant_id,
                resource_id,
                reason,
            )
            return str(row["id"])

        return await self._run(_release_active)

    async def list_orphaned_allocations(self) -> builtins.list[dict[str, Any]]:
        """跨租户列出归属资源（同租户、同 uid）已不存在的 Active 分配（含 ``devices``）。

        经 :meth:`request_delete` 删除的资源在物理删除之前释放分配；孤立的分配只来自绕过
        受控删除的 :meth:`delete`，或之前的版本留下的数据。
        """

        def _list_orphaned() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM allocations AS a WHERE a.status=? AND NOT EXISTS ("
                " SELECT 1 FROM resources AS r"
                " WHERE r.tenant_id=a.tenant_id AND r.uid=a.resource_id"
                ") ORDER BY a.created_at, a.id",
                (AllocationStatus.ACTIVE.value,),
            ).fetchall()
            return [self._allocation_record(row) for row in rows]

        return await self._run(_list_orphaned)

    async def expire_orphaned_allocation(self, allocation_id: str) -> bool:
        """把仍为 Active、归属资源仍不存在的分配标记为 ``Expired``，其设备随之空闲。

        检查与写入在同一条 UPDATE 中完成；分配已不是 Active 或归属资源存在时不修改，返回
        ``False``。
        """

        def _expire() -> bool:
            cur = self._conn.execute(
                "UPDATE allocations SET status=?, released_at=datetime('now') "
                "WHERE id=? AND status=? AND NOT EXISTS ("
                " SELECT 1 FROM resources AS r"
                " WHERE r.tenant_id=allocations.tenant_id AND r.uid=allocations.resource_id)",
                (AllocationStatus.EXPIRED.value, allocation_id, AllocationStatus.ACTIVE.value),
            )
            self._conn.commit()
            return cur.rowcount == 1

        return await self._run(_expire)

    async def release_allocation(
        self,
        allocation_id: str,
        tenant_id: str,
        reason: str | None = None,
    ) -> None:
        """将分配 ``Active → Released``（走 ``ALLOCATION_STATE_MACHINE`` 校验）。

        分配持有的设备随同一条 UPDATE 释放（触发器 ``trg_allocation_devices_follow_status``）。

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
        """列出租户的全部分配记录（含终态与各自的 ``devices``），强制租户过滤。"""

        def _list_allocations() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT * FROM allocations WHERE tenant_id=? ORDER BY created_at, id",
                (tenant_id,),
            ).fetchall()
            return [self._allocation_record(row) for row in rows]

        return await self._run(_list_allocations)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def ping(self) -> None:
        """探测连接可用（就绪探针使用）；连接已关闭或损坏时抛 ``sqlite3.Error``。"""

        def _ping() -> None:
            self._conn.execute("SELECT 1").fetchone()

        await self._run(_ping)

    async def close(self) -> None:
        """关闭数据库连接。"""
        await asyncio.to_thread(self._db.close)


__all__ = [
    "DEFAULT_RELEASE_FINALIZER",
    "SIDE_EFFECT_PHASES",
    "ChangeType",
    "ConflictReason",
    "InvalidPageTokenError",
    "RequestDeleteResult",
    "ResourceChange",
    "ResourceKey",
    "ResourcePage",
    "SQLiteRegistry",
]
