"""DecisionRecord 持久化与审批集成单元测试（对齐 development-plan 5.5 / IVT-11）。

覆盖：
* DecisionRecord 创建与校验；
* DecisionRecordStore save/get/list_by_tenant/list_pending_approvals/update_approval_status；
* NotFoundError when get 不存在；
* 审批状态流转。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from forgemesh.core.errors import NotFoundError
from forgemesh.decision.provider import DecisionCandidate
from forgemesh.decision.records import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    DecisionRecord,
    DecisionRecordStore,
)


def _make_candidate(cid: str = "c1") -> DecisionCandidate:
    """构造测试用候选。"""
    return DecisionCandidate(
        candidate_id=cid,
        action="schedule",
        parameters={"node": "node-1"},
        rationale=["best fit"],
        confidence=0.9,
    )


def _make_record(
    record_id: str = "rec-1",
    tenant_id: str = "tenant-a",
    approval_status: str | None = None,
    success: bool = True,
) -> DecisionRecord:
    """构造测试用决策记录。"""
    candidate = _make_candidate()
    return DecisionRecord(
        record_id=record_id,
        request_id="req-1",
        tenant_id=tenant_id,
        provider_id="decision-rules",
        provider_version="0.1.0",
        operation="inference.schedule",
        candidates=[candidate],
        chosen=candidate if success else None,
        success=success,
        risk_level="low",
        created_at=time.time(),
        approval_status=approval_status,
    )


# --------------------------------------------------------------------------- #
# DecisionRecord 创建与校验
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_decision_record_creation() -> None:
    """DecisionRecord 正常创建。"""
    record = _make_record()

    assert record.record_id == "rec-1"
    assert record.tenant_id == "tenant-a"
    assert record.provider_id == "decision-rules"
    assert record.operation == "inference.schedule"
    assert len(record.candidates) == 1
    assert record.chosen is not None
    assert record.success is True
    assert record.risk_level == "low"
    assert record.approval_status is None


@pytest.mark.unit
def test_decision_record_no_approval() -> None:
    """无需审批时 approval_status 为 None。"""
    record = _make_record(approval_status=None)
    assert record.approval_status is None


@pytest.mark.unit
def test_decision_record_with_pending_approval() -> None:
    """待审批记录。"""
    record = _make_record(approval_status=APPROVAL_PENDING)
    assert record.approval_status == APPROVAL_PENDING


# --------------------------------------------------------------------------- #
# DecisionRecordStore save/get
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_save_and_get() -> None:
    """保存后可查询。"""
    store = DecisionRecordStore()
    record = _make_record()

    asyncio.run(store.save(record))
    fetched = asyncio.run(store.get("rec-1"))

    assert fetched.record_id == record.record_id
    assert fetched.tenant_id == record.tenant_id
    assert fetched.operation == record.operation
    assert fetched.success == record.success
    assert len(fetched.candidates) == 1
    assert fetched.candidates[0].candidate_id == "c1"
    assert fetched.chosen is not None
    assert fetched.chosen.candidate_id == "c1"


@pytest.mark.unit
def test_get_not_found() -> None:
    """查询不存在的记录抛 NotFoundError。"""
    store = DecisionRecordStore()
    with pytest.raises(NotFoundError):
        asyncio.run(store.get("nonexistent"))


@pytest.mark.unit
def test_save_overwrite() -> None:
    """同一 record_id 重复保存覆盖。"""
    store = DecisionRecordStore()
    record = _make_record(success=True)
    asyncio.run(store.save(record))

    # 覆盖为失败记录。
    record2 = _make_record(success=False)
    asyncio.run(store.save(record2))

    fetched = asyncio.run(store.get("rec-1"))
    assert fetched.success is False
    assert fetched.chosen is None


# --------------------------------------------------------------------------- #
# list_by_tenant
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_list_by_tenant() -> None:
    """按租户列出记录。"""
    store = DecisionRecordStore()

    for i in range(3):
        asyncio.run(store.save(_make_record(record_id=f"rec-{i}", tenant_id="t1")))
    asyncio.run(store.save(_make_record(record_id="rec-other", tenant_id="t2")))

    records = asyncio.run(store.list_by_tenant("t1"))
    assert len(records) == 3
    assert all(r.tenant_id == "t1" for r in records)


@pytest.mark.unit
def test_list_by_tenant_limit() -> None:
    """limit 参数限制返回数量。"""
    store = DecisionRecordStore()
    for i in range(5):
        asyncio.run(store.save(_make_record(record_id=f"rec-{i}", tenant_id="t1")))

    records = asyncio.run(store.list_by_tenant("t1", limit=2))
    assert len(records) == 2


@pytest.mark.unit
def test_list_by_tenant_empty() -> None:
    """无记录的租户返回空列表。"""
    store = DecisionRecordStore()
    records = asyncio.run(store.list_by_tenant("no-such-tenant"))
    assert records == []


# --------------------------------------------------------------------------- #
# 审批状态流转
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_list_pending_approvals() -> None:
    """列出待审批记录。"""
    store = DecisionRecordStore()

    asyncio.run(store.save(_make_record(record_id="pending-1", approval_status=APPROVAL_PENDING)))
    asyncio.run(store.save(_make_record(record_id="pending-2", approval_status=APPROVAL_PENDING)))
    asyncio.run(store.save(_make_record(record_id="approved-1", approval_status=APPROVAL_APPROVED)))
    asyncio.run(store.save(_make_record(record_id="no-approval", approval_status=None)))

    pending = asyncio.run(store.list_pending_approvals("tenant-a"))
    pending_ids = {r.record_id for r in pending}

    assert "pending-1" in pending_ids
    assert "pending-2" in pending_ids
    assert "approved-1" not in pending_ids
    assert "no-approval" not in pending_ids


@pytest.mark.unit
def test_update_approval_status() -> None:
    """更新审批状态。"""
    store = DecisionRecordStore()
    asyncio.run(store.save(_make_record(record_id="rec-1", approval_status=APPROVAL_PENDING)))

    asyncio.run(store.update_approval_status("rec-1", APPROVAL_APPROVED))

    fetched = asyncio.run(store.get("rec-1"))
    assert fetched.approval_status == APPROVAL_APPROVED

    # 待审批列表中不再包含。
    pending = asyncio.run(store.list_pending_approvals("tenant-a"))
    assert all(r.record_id != "rec-1" for r in pending)


@pytest.mark.unit
def test_update_approval_status_reject() -> None:
    """审批拒绝流转。"""
    store = DecisionRecordStore()
    asyncio.run(store.save(_make_record(record_id="rec-1", approval_status=APPROVAL_PENDING)))

    asyncio.run(store.update_approval_status("rec-1", APPROVAL_REJECTED))

    fetched = asyncio.run(store.get("rec-1"))
    assert fetched.approval_status == APPROVAL_REJECTED


@pytest.mark.unit
def test_update_approval_status_not_found() -> None:
    """更新不存在的记录抛 NotFoundError。"""
    store = DecisionRecordStore()
    with pytest.raises(NotFoundError):
        asyncio.run(store.update_approval_status("no-such-rec", APPROVAL_APPROVED))


# --------------------------------------------------------------------------- #
# 租户隔离
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_list_pending_approvals_tenant_isolation() -> None:
    """待审批列表按租户隔离。"""
    store = DecisionRecordStore()
    asyncio.run(store.save(_make_record(record_id="t1-pending", tenant_id="t1", approval_status=APPROVAL_PENDING)))
    asyncio.run(store.save(_make_record(record_id="t2-pending", tenant_id="t2", approval_status=APPROVAL_PENDING)))

    t1_pending = asyncio.run(store.list_pending_approvals("t1"))
    assert len(t1_pending) == 1
    assert t1_pending[0].record_id == "t1-pending"
