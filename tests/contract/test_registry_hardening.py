"""Registry 加固回归测试：单实例并发、身份唯一性、受控删除 CAS、标签过滤。

每个用例对应一个已复现的缺陷，修复前稳定失败、修复后必须通过。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import ConflictError, PreconditionFailedError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.resource.allocation import Allocation

KIND = "Node"
TENANT = "tenant-a"


class _InterleavedWriter:
    """包装 sqlite3 连接：命中指定 SELECT 后立即插入一次已提交的写入。

    模拟另一个连接（另一副本 / CLI / 运维脚本）恰好在「读」与「写」之间完成了写入。
    """

    def __init__(self, inner: sqlite3.Connection, trigger: str, statement: str) -> None:
        self._inner = inner
        self._trigger = trigger
        self._statement = statement
        self.fired = False

    def _maybe_fire(self, sql: str) -> None:
        if not self.fired and sql.lstrip().startswith(self._trigger):
            self.fired = True
            self._inner.execute(self._statement)
            self._inner.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        result = self._inner.execute(sql, params)
        self._maybe_fire(sql)
        return result

    def cursor(self) -> _InterleavedCursor:
        return _InterleavedCursor(self, self._inner.cursor())

    def __getattr__(self, item: str):
        return getattr(self._inner, item)


class _InterleavedCursor:
    def __init__(self, writer: _InterleavedWriter, inner: sqlite3.Cursor) -> None:
        self._writer = writer
        self._inner = inner

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        result = self._inner.execute(sql, params)
        self._writer._maybe_fire(sql)
        return result

    def __getattr__(self, item: str):
        return getattr(self._inner, item)


def _make_resource(
    name: str,
    namespace: str | None = "ns-a",
    labels: dict[str, str] | None = None,
) -> Resource:
    return Resource(
        kind=KIND,
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            uid=f"uid-{name}",
            tenant_id=TENANT,
            labels=labels or {},
        ),
    )


@pytest.mark.contract
def test_shared_instance_concurrent_writes_are_not_lost() -> None:
    """同一实例并发写：失败方的 rollback 不得连带丢弃成功方的写入（缺陷 R-1）。"""

    async def scenario() -> None:
        reg = SQLiteRegistry(":memory:")
        try:
            seed = await reg.put(_make_resource("seed"))
            for round_no in range(10):
                names = [f"svc-{round_no}-{i}" for i in range(32)]
                creates = [reg.put(_make_resource(n)) for n in names]
                stale = [reg.put(seed, expected_version="999") for _ in range(32)]
                results = await asyncio.gather(*creates, *stale, return_exceptions=True)
                created, rejected = results[:32], results[32:]
                assert not [r for r in created if isinstance(r, BaseException)]
                assert all(isinstance(r, ConflictError) for r in rejected)
                stored = {r.metadata.name for r in await reg.list(KIND, TENANT, limit=10_000)}
                assert set(names) <= stored
        finally:
            await reg.close()

    asyncio.run(scenario())


@pytest.mark.contract
def test_create_without_namespace_rejects_duplicate_name() -> None:
    """namespace 为空的同名资源只能创建一次（SQLite 主键 NULL 互不相等）。"""

    async def scenario() -> None:
        reg = SQLiteRegistry(":memory:")
        try:
            await reg.put(_make_resource("cluster-scoped", namespace=None))
            with pytest.raises(ConflictError):
                await reg.put(_make_resource("cluster-scoped", namespace=None))
            assert len(await reg.list(KIND, TENANT)) == 1
        finally:
            await reg.close()

    asyncio.run(scenario())


@pytest.mark.contract
def test_open_refuses_legacy_db_with_duplicate_identities(tmp_path) -> None:
    """存量库已有重复身份时拒绝打开，而不是静默删除其中一条。"""
    db_file = tmp_path / "legacy.db"
    reg = SQLiteRegistry(str(db_file))
    asyncio.run(reg.close())

    conn = sqlite3.connect(db_file)
    conn.execute("DROP INDEX ux_resources_identity")
    for _ in range(2):
        conn.execute(
            "INSERT INTO resources (tenant_id, kind, namespace, name, body) "
            "VALUES (?, ?, NULL, ?, '{}')",
            (TENANT, KIND, "dup"),
        )
    conn.commit()
    conn.close()

    with pytest.raises(PreconditionFailedError, match="name=dup"):
        SQLiteRegistry(str(db_file))


@pytest.mark.contract
def test_request_delete_does_not_overwrite_concurrent_update() -> None:
    """受控删除的「置 Terminating」写入以读到的版本为条件，不覆盖并发更新。"""

    async def scenario() -> None:
        reg = SQLiteRegistry(":memory:")
        try:
            victim = _make_resource("victim")
            victim.finalizers = ["forgemesh.io/resource-release"]
            await reg.put(victim)
            conn = reg._conn
            reg._conn = _InterleavedWriter(
                conn,
                trigger="SELECT body, phase, finalizers, resource_version",
                statement=(
                    "UPDATE resources SET resource_version = resource_version + 1, "
                    "body = json_set(body, '$.metadata.labels.writer', 'concurrent', "
                    "'$.metadata.resourceVersion', '1') WHERE name = 'victim'"
                ),
            )
            with pytest.raises(ConflictError):
                await reg.request_delete(KIND, "ns-a", "victim", TENANT)
            reg._conn = conn

            current = await reg.get(KIND, "ns-a", "victim", TENANT)
            assert current.metadata.labels == {"writer": "concurrent"}
            assert current.metadata.resourceVersion == "1"

            retried = await reg.request_delete(KIND, "ns-a", "victim", TENANT)
            assert retried.deleted is False
            after = await reg.get(KIND, "ns-a", "victim", TENANT)
            assert after.metadata.labels == {"writer": "concurrent"}
            assert after.metadata.resourceVersion == "2"
        finally:
            await reg.close()

    asyncio.run(scenario())


@pytest.mark.contract
def test_request_delete_does_not_bypass_concurrently_added_finalizer() -> None:
    """物理删除以读到的版本为条件：并发追加的 finalizer 不能被绕过。"""

    async def scenario() -> None:
        reg = SQLiteRegistry(":memory:")
        try:
            await reg.put(_make_resource("victim"))
            conn = reg._conn
            reg._conn = _InterleavedWriter(
                conn,
                trigger="SELECT body, phase, finalizers, resource_version",
                statement=(
                    "UPDATE resources SET resource_version = resource_version + 1, "
                    "finalizers = '[\"guard\"]', "
                    "body = json_set(body, '$.finalizers', json('[\"guard\"]'), "
                    "'$.metadata.resourceVersion', '1') WHERE name = 'victim'"
                ),
            )
            with pytest.raises(ConflictError):
                await reg.request_delete(KIND, "ns-a", "victim", TENANT)
            reg._conn = conn

            survivor = await reg.get(KIND, "ns-a", "victim", TENANT)
            assert survivor.finalizers == ["guard"]
        finally:
            await reg.close()

    asyncio.run(scenario())


@pytest.mark.contract
def test_request_delete_does_not_drop_a_resource_that_just_got_an_allocation() -> None:
    """物理删除以「仍没有 Active 分配」为条件：检查之后并发获得的分配不会随删除泄漏。"""

    async def scenario() -> None:
        reg = SQLiteRegistry(":memory:")
        try:
            await reg.put(_make_resource("victim"))
            conn = reg._conn
            reg._conn = _InterleavedWriter(
                conn,
                trigger="SELECT 1 FROM allocations",
                statement=(
                    "INSERT INTO allocations (id, tenant_id, resource_id, status) "
                    "VALUES ('raced', 'tenant-a', 'uid-victim', 'Active')"
                ),
            )
            with pytest.raises(ConflictError):
                await reg.request_delete(KIND, "ns-a", "victim", TENANT)
            reg._conn = conn

            assert await reg.get(KIND, "ns-a", "victim", TENANT) is not None
            retried = await reg.request_delete(KIND, "ns-a", "victim", TENANT)
            assert retried.deleted is False
        finally:
            await reg.close()

    asyncio.run(scenario())


@pytest.mark.contract
def test_release_allocation_does_not_overwrite_terminal_state() -> None:
    """释放分配以读到的状态为条件：并发过期后不得被改写为 Released。"""

    async def scenario() -> None:
        reg = SQLiteRegistry(":memory:")
        try:
            allocation_id = await reg.put_allocation(
                Allocation(resource_id="gpu-0", tenant_id=TENANT, idempotency_key="alloc-1"),
            )
            conn = reg._conn
            reg._conn = _InterleavedWriter(
                conn,
                trigger="SELECT status FROM allocations",
                statement="UPDATE allocations SET status = 'Expired'",
            )
            with pytest.raises(ConflictError):
                await reg.release_allocation(allocation_id, TENANT)
            reg._conn = conn

            [record] = await reg.list_allocations(TENANT)
            assert record["status"] == "Expired"
        finally:
            await reg.close()

    asyncio.run(scenario())


@pytest.mark.contract
def test_label_selector_is_applied_before_limit() -> None:
    """标签过滤先于 LIMIT：匹配项排在第一页之外时也必须被返回且可分页遍历。"""

    async def scenario() -> None:
        reg = SQLiteRegistry(":memory:")
        try:
            for i in range(150):
                labels = {"app.kubernetes.io/name": "target"} if i >= 140 else {}
                await reg.put(_make_resource(f"res-{i:03d}", labels=labels))
            selector = {"app.kubernetes.io/name": "target"}

            first_page = await reg.list(KIND, TENANT, labels=selector, limit=100)
            assert [r.metadata.name for r in first_page] == [
                f"res-{i:03d}" for i in range(140, 150)
            ]

            seen: list[str] = []
            token = None
            while True:
                page = await reg.list(KIND, TENANT, labels=selector, limit=4, continue_token=token)
                if not page:
                    break
                seen.extend(r.metadata.name for r in page)
                last = page[-1].metadata
                token = SQLiteRegistry._encode_token(last.namespace, last.name)
            assert seen == [f"res-{i:03d}" for i in range(140, 150)]
        finally:
            await reg.close()

    asyncio.run(scenario())


@pytest.mark.contract
@pytest.mark.parametrize("limit", [0, -1])
def test_list_rejects_non_positive_limit(limit: int) -> None:
    """SQLite 中 LIMIT -1 表示不限条数，必须在入口拒绝。"""
    reg = SQLiteRegistry(":memory:")
    with pytest.raises(ValueError):
        asyncio.run(reg.list(KIND, TENANT, limit=limit))
