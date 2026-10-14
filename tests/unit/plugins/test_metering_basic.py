"""metering-basic 插件单元测试（对齐 development-plan WP-4.2）。

覆盖：

* ``UsageRecord`` 创建与校验（``extra="forbid"``）。
* ``UsageStore.record`` 追加 / ``get`` 查询 / 不存在抛 ``NotFoundError``。
* ``list_by_tenant`` 按租户查询（时间范围过滤、limit）。
* ``aggregate`` 聚合统计（总 Token / 成功失败数 / 延迟）。
* ``correct`` 计量修正（原记录保留 + 新增 ``correction_of`` 记录，IVT-10）。
* 追加式不可修改（重复 ``record_id`` 抛 ``ConflictError``）。
* 多租户隔离。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.core.errors import ConflictError, NotFoundError
from metering_basic_plugin import UsageRecord, UsageStore, UsageSummary


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """为每个测试提供干净的 current event loop。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(coro):
    """在当前 event loop 上运行协程并返回结果。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def _record(**overrides: object) -> UsageRecord:
    """构造使用记录，提供合理默认值。"""
    kwargs: dict[str, object] = {
        "record_id": "rec-1",
        "tenant_id": "tenant-a",
        "request_id": "req-1",
        "model_id": "model-1",
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_tokens": 10,
        "success": True,
        "latency_ms": 200.0,
        "correlation_id": "corr-1",
        "created_at": 1000.0,
    }
    kwargs.update(overrides)
    return UsageRecord(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# UsageRecord
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_usage_record_creation_with_defaults() -> None:
    """UsageRecord 应正确创建，correction_of 默认为 None。"""
    rec = _record()
    assert rec.record_id == "rec-1"
    assert rec.tenant_id == "tenant-a"
    assert rec.input_tokens == 100
    assert rec.output_tokens == 50
    assert rec.cached_tokens == 10
    assert rec.success is True
    assert rec.latency_ms == 200.0
    assert rec.correction_of is None


@pytest.mark.unit
def test_usage_record_rejects_extra_fields() -> None:
    """extra="forbid" 应拒绝多余字段。"""
    with pytest.raises(ValueError):  # noqa: PT011 — pydantic ValidationError 子类
        _record(unknown_field="bad")


@pytest.mark.unit
def test_usage_record_correction_of_set_explicitly() -> None:
    """correction_of 可显式设置（指向原记录 ID）。"""
    rec = _record(record_id="rec-fix", correction_of="rec-orig")
    assert rec.correction_of == "rec-orig"


# --------------------------------------------------------------------------- #
# UsageStore: record / get
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_record_and_get_roundtrip() -> None:
    """record 追加后 get 应返回等价记录。"""
    store = UsageStore()
    rec = _record()
    _run(store.record(rec))

    fetched = _run(store.get("rec-1"))
    assert fetched == rec


@pytest.mark.unit
def test_get_not_found_raises() -> None:
    """查询不存在的记录应抛 NotFoundError。"""
    store = UsageStore()
    with pytest.raises(NotFoundError):
        _run(store.get("no-such-record"))


@pytest.mark.unit
def test_duplicate_record_raises_conflict() -> None:
    """重复 record 同一 record_id 应抛 ConflictError（追加式不可修改）。"""
    store = UsageStore()
    _run(store.record(_record()))
    with pytest.raises(ConflictError):
        _run(store.record(_record()))


# --------------------------------------------------------------------------- #
# UsageStore: list_by_tenant
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_by_tenant_filters_by_tenant() -> None:
    """list_by_tenant 只返回指定租户的记录。"""
    store = UsageStore()
    _run(store.record(_record(record_id="r1", tenant_id="a", created_at=100.0)))
    _run(store.record(_record(record_id="r2", tenant_id="b", created_at=200.0)))
    _run(store.record(_record(record_id="r3", tenant_id="a", created_at=300.0)))

    result = _run(store.list_by_tenant("a"))
    assert [r.record_id for r in result] == ["r1", "r3"]


@pytest.mark.unit
def test_list_by_tenant_time_range_filter() -> None:
    """list_by_tenant 应按 [start, end) 过滤时间范围并升序返回。"""
    store = UsageStore()
    for rid, ts in [("r1", 100.0), ("r2", 200.0), ("r3", 300.0), ("r4", 400.0)]:
        _run(store.record(_record(record_id=rid, created_at=ts)))

    # [200, 400) → r2, r3
    result = _run(store.list_by_tenant("tenant-a", start=200.0, end=400.0))
    assert [r.record_id for r in result] == ["r2", "r3"]


@pytest.mark.unit
def test_list_by_tenant_limit() -> None:
    """list_by_tenant 应尊重 limit 参数。"""
    store = UsageStore()
    for i in range(5):
        _run(store.record(_record(record_id=f"r{i}", created_at=100.0 + i)))

    result = _run(store.list_by_tenant("tenant-a", limit=3))
    assert len(result) == 3
    assert [r.record_id for r in result] == ["r0", "r1", "r2"]


@pytest.mark.unit
def test_list_by_tenant_empty() -> None:
    """无记录时 list_by_tenant 返回空列表。"""
    store = UsageStore()
    result = _run(store.list_by_tenant("nobody"))
    assert result == []


# --------------------------------------------------------------------------- #
# UsageStore: aggregate
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_aggregate_statistics() -> None:
    """aggregate 应正确统计总 Token / 成功失败数 / 延迟。"""
    store = UsageStore()
    _run(
        store.record(
            _record(
                record_id="r1",
                input_tokens=100,
                output_tokens=50,
                cached_tokens=10,
                success=True,
                latency_ms=200.0,
                created_at=1000.0,
            )
        )
    )
    _run(
        store.record(
            _record(
                record_id="r2",
                input_tokens=200,
                output_tokens=100,
                cached_tokens=20,
                success=True,
                latency_ms=400.0,
                created_at=1500.0,
            )
        )
    )
    _run(
        store.record(
            _record(
                record_id="r3",
                input_tokens=50,
                output_tokens=20,
                cached_tokens=0,
                success=False,
                latency_ms=100.0,
                created_at=1800.0,
            )
        )
    )

    summary = _run(store.aggregate("tenant-a", 1000.0, 2000.0))
    assert isinstance(summary, UsageSummary)
    assert summary.tenant_id == "tenant-a"
    assert summary.record_count == 3
    assert summary.total_input_tokens == 350
    assert summary.total_output_tokens == 170
    assert summary.total_cached_tokens == 30
    assert summary.success_count == 2
    assert summary.failure_count == 1
    assert summary.total_latency_ms == 700.0
    assert summary.avg_latency_ms == pytest.approx(700.0 / 3)


@pytest.mark.unit
def test_aggregate_time_range_excludes_outside() -> None:
    """aggregate 应只统计 [start, end) 范围内的记录。"""
    store = UsageStore()
    _run(store.record(_record(record_id="r1", input_tokens=100, created_at=100.0)))
    _run(store.record(_record(record_id="r2", input_tokens=200, created_at=200.0)))
    _run(store.record(_record(record_id="r3", input_tokens=300, created_at=300.0)))

    summary = _run(store.aggregate("tenant-a", 200.0, 300.0))
    assert summary.record_count == 1
    assert summary.total_input_tokens == 200


@pytest.mark.unit
def test_aggregate_empty_returns_zeros() -> None:
    """无记录时 aggregate 应返回全零统计。"""
    store = UsageStore()
    summary = _run(store.aggregate("tenant-a", 0.0, 1000.0))
    assert summary.record_count == 0
    assert summary.total_input_tokens == 0
    assert summary.total_output_tokens == 0
    assert summary.total_cached_tokens == 0
    assert summary.success_count == 0
    assert summary.failure_count == 0
    assert summary.total_latency_ms == 0.0
    assert summary.avg_latency_ms == 0.0


# --------------------------------------------------------------------------- #
# UsageStore: correct — 计量修正（IVT-10）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_correct_preserves_original_and_adds_correction() -> None:
    """correct 应保留原记录并新增 correction_of 记录（IVT-10）。"""
    store = UsageStore()
    original = _record(record_id="rec-orig", input_tokens=100, created_at=1000.0)
    _run(store.record(original))

    correction = _record(
        record_id="rec-fix",
        input_tokens=120,
        created_at=1100.0,
    )
    _run(store.correct("rec-orig", correction))

    # 原记录保留不变
    orig_fetched = _run(store.get("rec-orig"))
    assert orig_fetched.input_tokens == 100
    assert orig_fetched.correction_of is None

    # 修正记录存在且 correction_of 指向原记录
    fix_fetched = _run(store.get("rec-fix"))
    assert fix_fetched.input_tokens == 120
    assert fix_fetched.correction_of == "rec-orig"


@pytest.mark.unit
def test_correct_original_not_found_raises() -> None:
    """修正不存在的原记录应抛 NotFoundError。"""
    store = UsageStore()
    correction = _record(record_id="rec-fix")
    with pytest.raises(NotFoundError):
        _run(store.correct("no-such-original", correction))


@pytest.mark.unit
def test_correct_duplicate_correction_id_raises_conflict() -> None:
    """修正记录的 record_id 已存在应抛 ConflictError。"""
    store = UsageStore()
    _run(store.record(_record(record_id="rec-orig", created_at=1000.0)))
    _run(store.record(_record(record_id="rec-fix", created_at=1100.0)))

    with pytest.raises(ConflictError):
        _run(store.correct("rec-orig", _record(record_id="rec-fix")))


# --------------------------------------------------------------------------- #
# 多租户隔离
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_multi_tenant_isolation() -> None:
    """不同租户的记录互不可见（list / aggregate 均隔离）。"""
    store = UsageStore()
    _run(
        store.record(
            _record(
                record_id="a1",
                tenant_id="alpha",
                input_tokens=100,
                created_at=100.0,
            )
        )
    )
    _run(
        store.record(
            _record(
                record_id="b1",
                tenant_id="beta",
                input_tokens=200,
                created_at=100.0,
            )
        )
    )

    alpha_list = _run(store.list_by_tenant("alpha"))
    assert [r.record_id for r in alpha_list] == ["a1"]

    beta_list = _run(store.list_by_tenant("beta"))
    assert [r.record_id for r in beta_list] == ["b1"]

    alpha_sum = _run(store.aggregate("alpha", 0.0, 1000.0))
    assert alpha_sum.total_input_tokens == 100
    assert alpha_sum.record_count == 1

    beta_sum = _run(store.aggregate("beta", 0.0, 1000.0))
    assert beta_sum.total_input_tokens == 200
    assert beta_sum.record_count == 1


# --------------------------------------------------------------------------- #
# close
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_close_does_not_raise() -> None:
    """close 应正常关闭连接，不抛异常。"""
    store = UsageStore()
    _run(store.close())
