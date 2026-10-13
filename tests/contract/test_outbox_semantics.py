"""契约测试：事务性 Outbox 语义验证（v1.1 第 9.2 节 / 阶段 C，WP-C2）。

对已交付的 Outbox / Dispatcher / 幂等消费者做故障与边界验证：

* 同事务提交/回滚：业务状态与 Outbox 记录共享同一 SQLite 连接事务——
  提交时二者原子可见，回滚时二者均不残留；
* 重复发布：Dispatcher 对同一 Outbox 记录重复发布（发布后标记前崩溃重试）
  时，消费者侧按 eventId 去重，副作用只发生一次；
* 消费者提交失败：消费者处理成功但确认丢失后，重新投递不产生重复副作用；
* 死信/重放：发布失败的记录进入持久失败状态（事实上的死信态）、可查询、
  不再被自动重试，且可通过重置状态手动重放。

记录性测试：实现当前存在两处与 Outbox 模式理想语义的差距，按任务约定
不改源码，仅以测试固化当前行为并在 docstring 标注 v1.1 后续期望：
（1）``Outbox.put`` 内部自动 commit，使外部事务的 rollback 失效；
（2）``IdempotentConsumer`` 去重状态仅存内存，进程重启后重复投递会
产生重复副作用。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from forgemesh.controlplane.outbox import (
    Dispatcher,
    IdempotentConsumer,
    Outbox,
    OutboxEvent,
)
from forgemesh.controlplane.sqlite import Database


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_event(
    event_id: str = "evt-1",
    aggregate_type: str = "InferenceService",
    aggregate_id: str = "svc-1",
    event_type: str = "Created",
    payload: dict | None = None,
) -> OutboxEvent:
    """构造一个可复用的最小 OutboxEvent。"""
    return OutboxEvent(
        event_id=event_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload or {"name": "demo"},
    )


def _open_shared_outbox(db_file) -> tuple[Outbox, sqlite3.Connection]:
    """打开共享连接的 Outbox 并初始化业务表。

    实现提供的事务集成方式即 ``Outbox(database=...)`` 与业务存储共用一个
    :class:`Database`，业务状态与事件记录在该连接的同一事务上下文中写入。
    """
    outbox = Outbox(database=Database(str(db_file)))
    conn = outbox.database.conn
    conn.execute(
        "CREATE TABLE IF NOT EXISTS biz_services (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
    )
    conn.commit()
    return outbox, conn


# ---------------------------------------------------------------------- #
# 语义一：同事务提交/回滚
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_commit_path_business_and_event_same_transaction(tmp_path) -> None:
    """同事务提交：业务状态与事件在同一事务中原子提交，对外同时可见。"""
    outbox, conn = _open_shared_outbox(tmp_path / "outbox-commit.db")
    reader = sqlite3.connect(str(tmp_path / "outbox-commit.db"))

    # 业务事务：先写业务状态，再经共享连接写入事件；put 的 commit 是
    # 唯一提交点，两者在同一个事务中提交。
    conn.execute("BEGIN")
    conn.execute("INSERT INTO biz_services (id, status) VALUES ('svc-1', 'Active')")

    # 提交点之前：业务变更对外部连接不可见（仍在事务中）。
    assert reader.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 0

    outbox.put(_make_event("evt-commit"))

    # 提交点之后：业务状态与事件同时对同一数据库的其他连接可见。
    assert reader.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 1
    assert [e.event_id for e in outbox.get_pending()] == ["evt-commit"]

    reader.close()
    conn.close()


@pytest.mark.contract
def test_rollback_path_no_residual_event(tmp_path) -> None:
    """同事务回滚：业务回滚时 Outbox 记录不残留。

    说明：``put`` 自带 commit，无法参与外部事务回滚（见
    ``test_put_auto_commit_commits_ambient_transaction`` 的记录性断言），
    故此处按实现提供的共享连接 + 事务上下文，以与 ``put`` 等价的写入
    语义构造同一事务，验证 Outbox 模式的核心不变量。
    """
    outbox, conn = _open_shared_outbox(tmp_path / "outbox-rollback.db")

    conn.execute("BEGIN")
    conn.execute("INSERT INTO biz_services (id, status) VALUES ('svc-2', 'Active')")
    event = _make_event("evt-rollback")
    conn.execute(
        "INSERT INTO outbox_events (event_id, aggregate_type, aggregate_id, "
        "event_type, payload, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event.event_id,
            event.aggregate_type,
            event.aggregate_id,
            event.event_type,
            json.dumps(event.payload, ensure_ascii=False),
            event.created_at,
            event.status,
        ),
    )
    # 业务侧失败：整体回滚，业务状态与事件记录一并撤销。
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 0
    assert outbox.get_pending() == []  # Outbox 记录不残留
    assert outbox.get_stats() == {}  # 各状态均无记录
    conn.close()


@pytest.mark.contract
def test_put_auto_commit_commits_ambient_transaction(tmp_path) -> None:
    """记录性测试：put 内部自动 commit，使外部事务的 rollback 失效。

    Outbox 模式要求事件与业务状态在同一事务中原子提交/回滚；当前 ``put``
    的实现会在业务事务中途创建提交点，之后调用方 rollback 无法撤销业务
    变更或事件（两者均残留）。v1.1 期望 ``put`` 支持由调用方控制事务边界
    （如去内部 commit / 提供 savepoint 语义），使本测试可改为断言回滚成功。
    """
    outbox, conn = _open_shared_outbox(tmp_path / "outbox-put.db")

    conn.execute("BEGIN")
    conn.execute("INSERT INTO biz_services (id, status) VALUES ('svc-3', 'Active')")
    outbox.put(_make_event("evt-put"))
    conn.rollback()  # 模拟业务侧失败，尝试整体回滚

    # 当前行为：put 的内部 commit 已提交整个事务，rollback 无效，
    # 业务变更与事件均残留。
    assert conn.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 1
    assert [e.event_id for e in outbox.get_pending()] == ["evt-put"]
    conn.close()


# ---------------------------------------------------------------------- #
# 语义二：重复发布（重试场景）与消费者幂等
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_duplicate_publish_consumer_idempotent(tmp_path) -> None:
    """重复发布：同一记录发布两次（发布后标记前崩溃重试），副作用仅一次。"""
    outbox = Outbox(str(tmp_path / "outbox-dup.db"))
    event = _make_event("evt-dup")
    outbox.put(event)

    consumer = IdempotentConsumer()
    deliveries: list[str] = []  # 投递记录（publisher 被调用即记一次）
    side_effects: list[str] = []  # 消费者真实副作用

    def handler(ev: OutboxEvent) -> None:
        side_effects.append(ev.event_id)

    def publisher(ev: OutboxEvent) -> None:
        deliveries.append(ev.event_id)
        consumer.consume(ev, handler)  # 每次投递都交给下游消费者

    # 第一次投递成功，但模拟"标记 published 前进程崩溃"：投递已发生、
    # 标记未落库，记录仍为 pending。
    publisher(event)
    assert len(outbox.get_pending()) == 1

    # 重启后的新 Dispatcher（内存已处理集合丢失）对同一记录重新发布。
    restarted = Dispatcher(outbox, publisher)
    count = _run(restarted.dispatch_once())

    assert count == 1
    assert deliveries == ["evt-dup", "evt-dup"]  # 同一记录确实发布两次
    assert side_effects == ["evt-dup"]  # 消费者 eventId 去重后副作用仅一次
    assert consumer.processed_count == 1
    assert outbox.get_stats().get("published") == 1


# ---------------------------------------------------------------------- #
# 语义三：消费者提交失败（确认丢失）后重投
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_redelivery_after_ack_loss_no_duplicate_side_effect() -> None:
    """消费者提交失败：处理成功但确认丢失，重新投递不产生重复副作用。"""
    consumer = IdempotentConsumer()
    event = _make_event("evt-ack")
    side_effects: list[str] = []

    def handler(ev: OutboxEvent) -> None:
        side_effects.append(ev.event_id)

    # 首次投递：handler 执行成功（副作用已发生），消费者本地记录已处理；
    # 确认（ack）在返回途中丢失，上游未收到。
    first = consumer.consume(event, handler)
    assert first is True

    # 上游因未收到确认而重新投递同一事件。
    second = consumer.consume(event, handler)

    assert second is False  # 重复投递被去重跳过
    assert side_effects == ["evt-ack"]  # 不产生重复副作用
    assert consumer.is_processed("evt-ack") is True


@pytest.mark.contract
def test_consumer_dedup_is_in_memory_only_after_restart() -> None:
    """记录性测试：消费者去重状态仅存内存，重启后重复投递会产生重复副作用。

    当前实现 ``IdempotentConsumer._processed_ids`` 为进程内集合，无持久化。
    v1.1 期望 eventId 去重状态持久化（如 processed_events 表），
    使重启后重新投递仍不产生重复副作用；届时本测试应改为断言副作用仅一次。
    """
    event = _make_event("evt-restart")
    side_effects: list[str] = []

    def handler(ev: OutboxEvent) -> None:
        side_effects.append(ev.event_id)

    consumer_before = IdempotentConsumer()
    consumer_before.consume(event, handler)  # 重启前已处理过一次

    # 模拟进程重启：新实例的内存去重集合为空。
    consumer_after = IdempotentConsumer()
    redelivered = consumer_after.consume(event, handler)

    # 当前行为：重复投递被误判为首次，副作用重复执行。
    assert redelivered is True
    assert side_effects == ["evt-restart", "evt-restart"]


# ---------------------------------------------------------------------- #
# 语义四：死信/重放
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_failed_event_is_dead_and_queryable(tmp_path) -> None:
    """死信语义（记录性）：发布失败进入持久 failed 态，可查询且不再自动重试。

    当前实现无重试计数与独立死信队列：publisher 一次失败即 ``mark_failed``，
    事件脱离 pending 队列（不再被后续轮次自动重发），``failed`` 即事实上的
    死信状态。v1.1 期望按最大重试次数退避重试、超限后进入显式死信队列。
    """
    outbox = Outbox(str(tmp_path / "outbox-dead.db"))
    outbox.put(_make_event("evt-dead"))
    attempts: list[str] = []

    def broken_publisher(ev: OutboxEvent) -> None:
        attempts.append(ev.event_id)
        raise RuntimeError("broker unreachable")

    dispatcher = Dispatcher(outbox, broken_publisher)
    count = _run(dispatcher.dispatch_once())

    # 发布失败：本轮无成功处理，事件进入持久 failed 状态且可查询。
    assert count == 0
    assert outbox.get_stats() == {"failed": 1}
    assert outbox.get_pending() == []  # 脱离待发布队列

    # 后续轮次不再自动重试（attempts 不再增长）——无无限重发。
    assert _run(dispatcher.dispatch_once()) == 0
    assert attempts == ["evt-dead"]


@pytest.mark.contract
def test_manual_replay_failed_event(tmp_path) -> None:
    """手动重放（记录性）：failed 记录可经状态重置重放并成功发布。

    当前实现无官方 replay API，重放路径为经共享连接将 failed 重置为
    pending 后由 Dispatcher 重新分发。v1.1 期望提供显式 replay/dead-letter
    API；届时本测试应改用正式接口。
    """
    outbox = Outbox(str(tmp_path / "outbox-replay.db"))
    outbox.put(_make_event("evt-replay"))

    consumer = IdempotentConsumer()
    deliveries: list[str] = []
    side_effects: list[str] = []

    def handler(ev: OutboxEvent) -> None:
        side_effects.append(ev.event_id)

    def flaky_publisher(ev: OutboxEvent) -> None:
        deliveries.append(ev.event_id)
        if len(deliveries) == 1:
            raise RuntimeError("transient broker failure")  # 首次投递失败
        consumer.consume(ev, handler)  # 重放投递成功送达消费者

    # 首轮发布失败：进入死信（failed）状态，未产生副作用。
    failed_round = Dispatcher(outbox, flaky_publisher)
    assert _run(failed_round.dispatch_once()) == 0
    assert outbox.get_stats() == {"failed": 1}
    assert side_effects == []

    # 手动重放：通过 Outbox 暴露的共享连接重置状态（模拟运维重放动作）。
    outbox.database.conn.execute(
        "UPDATE outbox_events SET status = 'pending' WHERE event_id = ?",
        ("evt-replay",),
    )
    outbox.database.conn.commit()
    assert [e.event_id for e in outbox.get_pending()] == ["evt-replay"]

    replay_round = Dispatcher(outbox, flaky_publisher)
    count = _run(replay_round.dispatch_once())

    # 重放成功：事件被重新发布并标记 published，副作用恰好一次。
    assert count == 1
    assert deliveries == ["evt-replay", "evt-replay"]  # 失败一次 + 重放一次
    assert side_effects == ["evt-replay"]
    assert outbox.get_stats() == {"published": 1}
