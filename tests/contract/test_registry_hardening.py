"""Registry 加固回归测试：单实例并发、身份唯一性、受控删除 CAS、标签过滤。

每个用例对应一个已复现的缺陷，修复前稳定失败、修复后必须通过。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import ConflictError
from forgemesh.core.objects import ObjectMeta, Resource

KIND = "Node"
TENANT = "tenant-a"


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
