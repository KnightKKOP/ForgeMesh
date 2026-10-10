"""审批等待与 commit 重校验单元测试（对齐 development-plan 5.4）。

覆盖：
* 等待/通过/拒绝/超时/is_pending；
* 注册/校验通过/校验失败。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.workflowruntime.approval import ApprovalGate, CommitRevalidator


# --------------------------------------------------------------------------- #
# ApprovalGate — 等待与通过
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_wait_then_approve() -> None:
    """等待审批后通过信号唤醒，返回 True。"""
    gate = ApprovalGate()

    async def scenario() -> bool:
        # 先启动等待任务，再发送通过信号。
        wait_task = asyncio.create_task(gate.wait_for_approval("r1", "s1"))
        await asyncio.sleep(0.05)  # 确保等待已启动
        assert gate.is_pending("r1", "s1")
        gate.approve("r1", "s1")
        return await wait_task

    result = asyncio.run(scenario())
    assert result is True
    assert not gate.is_pending("r1", "s1")


@pytest.mark.unit
def test_approve_before_wait() -> None:
    """先通过再等待，立即返回 True。"""
    gate = ApprovalGate()
    gate.approve("r1", "s1")

    result = asyncio.run(gate.wait_for_approval("r1", "s1"))
    assert result is True
    assert not gate.is_pending("r1", "s1")


# --------------------------------------------------------------------------- #
# 拒绝
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_wait_then_reject() -> None:
    """等待审批后拒绝信号唤醒，返回 False。"""
    gate = ApprovalGate()

    async def scenario() -> bool:
        wait_task = asyncio.create_task(gate.wait_for_approval("r1", "s1"))
        await asyncio.sleep(0.05)
        gate.reject("r1", "s1")
        return await wait_task

    result = asyncio.run(scenario())
    assert result is False
    assert not gate.is_pending("r1", "s1")


@pytest.mark.unit
def test_reject_before_wait() -> None:
    """先拒绝再等待，立即返回 False。"""
    gate = ApprovalGate()
    gate.reject("r1", "s1")

    result = asyncio.run(gate.wait_for_approval("r1", "s1"))
    assert result is False


# --------------------------------------------------------------------------- #
# 超时
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_wait_timeout() -> None:
    """等待超时返回 False。"""
    gate = ApprovalGate()

    result = asyncio.run(gate.wait_for_approval("r1", "s1", timeout=0.1))
    assert result is False


@pytest.mark.unit
def test_wait_timeout_still_pending() -> None:
    """超时后审批条目仍存在（但 approved 仍为 None）。"""
    gate = ApprovalGate()

    asyncio.run(gate.wait_for_approval("r1", "s1", timeout=0.05))
    # 超时后 is_pending 仍为 True（因为未收到通过/拒绝信号）。
    assert gate.is_pending("r1", "s1")

    # 后续仍可通过 approve 唤醒。
    gate.approve("r1", "s1")
    assert not gate.is_pending("r1", "s1")


# --------------------------------------------------------------------------- #
# is_pending
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_is_pending_no_entry() -> None:
    """未发起等待的步骤 is_pending 返回 False。"""
    gate = ApprovalGate()
    assert not gate.is_pending("r1", "s1")


@pytest.mark.unit
def test_is_pending_after_wait_started() -> None:
    """等待启动后 is_pending 返回 True。"""
    gate = ApprovalGate()

    async def scenario() -> None:
        task = asyncio.create_task(gate.wait_for_approval("r1", "s1", timeout=1.0))
        await asyncio.sleep(0.05)
        assert gate.is_pending("r1", "s1")
        gate.approve("r1", "s1")
        await task

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 不同 run_id / step_id 隔离
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_independent_entries() -> None:
    """不同 (run_id, step_id) 的审批相互独立。"""
    gate = ApprovalGate()
    gate.approve("r1", "s1")
    gate.reject("r2", "s2")

    r1 = asyncio.run(gate.wait_for_approval("r1", "s1"))
    r2 = asyncio.run(gate.wait_for_approval("r2", "s2"))

    assert r1 is True
    assert r2 is False


# --------------------------------------------------------------------------- #
# CommitRevalidator
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_revalidate_no_validator() -> None:
    """未注册校验器的步骤默认通过。"""
    revalidator = CommitRevalidator()
    assert revalidator.revalidate("s1", {"version": 1}) is True


@pytest.mark.unit
def test_register_and_revalidate_pass() -> None:
    """注册校验器后重校验通过。"""
    revalidator = CommitRevalidator()

    def validator(context: dict[str, Any]) -> bool:
        return context.get("version") == context.get("expected_version")

    revalidator.register_validator("s1", validator)
    assert revalidator.revalidate("s1", {"version": 2, "expected_version": 2}) is True


@pytest.mark.unit
def test_revalidate_fail() -> None:
    """重校验失败（状态已变）返回 False。"""
    revalidator = CommitRevalidator()

    def validator(context: dict[str, Any]) -> bool:
        return context.get("version") == context.get("expected_version")

    revalidator.register_validator("s1", validator)
    # 版本已漂移。
    assert revalidator.revalidate("s1", {"version": 3, "expected_version": 2}) is False


@pytest.mark.unit
def test_revalidate_multiple_steps() -> None:
    """多个步骤各自独立的校验器。"""
    revalidator = CommitRevalidator()

    def always_true(context: dict[str, Any]) -> bool:
        return True

    def always_false(context: dict[str, Any]) -> bool:
        return False

    revalidator.register_validator("s1", always_true)
    revalidator.register_validator("s2", always_false)

    assert revalidator.revalidate("s1", {}) is True
    assert revalidator.revalidate("s2", {}) is False
    # s3 无校验器，默认通过。
    assert revalidator.revalidate("s3", {}) is True
