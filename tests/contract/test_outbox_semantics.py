"""契约测试：事务性 Outbox 语义验证（v1.1 第 9.2 节 / 阶段 C，WP-C2）。

对已交付的 Outbox / Dispatcher / 幂等消费者做故障与边界验证：

* 同事务提交/回滚：业务状态与 Outbox 记录共享同一 SQLite 连接事务——
  提交时二者原子可见，回滚时二者均不残留；
* 重复发布：Dispatcher 对同一 Outbox 记录重复发布（发布后标记前崩溃重试）
  时，消费者侧按 eventId 去重，副作用只发生一次；
* 消费者提交失败：消费者处理成功但确认丢失后，重新投递不产生重复副作用；
  去重记录持久化，消费者重启后同样成立；
* 退避重试与死信：发布失败按指数退避重试，目标恢复后最终送达；失败次数达到
  上限后进入 ``dead``、可查询、不再自动重试，可经 ``redrive`` 重新投递；
* 重启：发布状态只在数据库中，已发布的事件不会再次发布。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from forgemesh.controlplane.outbox import (
    Dispatcher,
    IdempotentConsumer,
    Outbox,
    OutboxEvent,
    RetryPolicy,
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


def _put(outbox: Outbox, event: OutboxEvent) -> None:
    """单独写入一个事件（put 不自行提交，由事务提交）。"""
    with outbox.database.transaction():
        outbox.put(event)


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

    # 业务事务：先写业务状态，再经共享连接写入事件；事务结束是唯一提交点。
    with outbox.database.transaction():
        conn.execute("INSERT INTO biz_services (id, status) VALUES ('svc-1', 'Active')")
        outbox.put(_make_event("evt-commit"))

        # 提交点之前：业务变更与事件对外部连接都不可见（仍在事务中）。
        assert reader.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0] == 0

    # 提交点之后：业务状态与事件同时对同一数据库的其他连接可见。
    assert reader.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 1
    assert [e.event_id for e in outbox.get_pending()] == ["evt-commit"]

    reader.close()
    conn.close()


@pytest.mark.contract
def test_rollback_path_no_residual_event(tmp_path) -> None:
    """同事务回滚：业务写入在事件之后失败时，业务状态与事件记录一并撤销。"""
    outbox, conn = _open_shared_outbox(tmp_path / "outbox-rollback.db")

    with pytest.raises(sqlite3.IntegrityError), outbox.database.transaction():
        conn.execute("INSERT INTO biz_services (id, status) VALUES ('svc-2', 'Active')")
        outbox.put(_make_event("evt-rollback"))
        # 业务侧失败（主键冲突）：整个事务回滚。
        conn.execute("INSERT INTO biz_services (id, status) VALUES ('svc-2', 'Active')")

    assert conn.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 0
    assert outbox.get_pending() == []  # Outbox 记录不残留
    assert outbox.get_stats() == {}  # 各状态均无记录
    conn.close()


@pytest.mark.contract
def test_put_leaves_the_commit_to_the_caller(tmp_path) -> None:
    """put 不在业务事务中途提交：调用方 rollback 同时撤销业务变更与事件。"""
    outbox, conn = _open_shared_outbox(tmp_path / "outbox-put.db")

    conn.execute("BEGIN")
    conn.execute("INSERT INTO biz_services (id, status) VALUES ('svc-3', 'Active')")
    outbox.put(_make_event("evt-put"))
    assert conn.in_transaction
    conn.rollback()  # 模拟业务侧失败，整体回滚

    assert conn.execute("SELECT COUNT(*) FROM biz_services").fetchone()[0] == 0
    assert outbox.get_pending() == []
    conn.close()


@pytest.mark.contract
def test_put_outside_a_transaction_is_rejected(tmp_path) -> None:
    """事务之外的 put 被拒绝：不会留下无人提交、随后被别的提交连带提交的事件。"""
    outbox, conn = _open_shared_outbox(tmp_path / "outbox-no-tx.db")

    with pytest.raises(RuntimeError, match="inside the caller's transaction"):
        outbox.put(_make_event("evt-orphan"))

    assert not conn.in_transaction
    assert outbox.get_stats() == {}
    conn.close()


# ---------------------------------------------------------------------- #
# 语义二：重复发布（重试场景）与消费者幂等
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_duplicate_publish_consumer_idempotent(tmp_path) -> None:
    """重复发布：同一记录发布两次（发布后标记前崩溃重试），副作用仅一次。"""
    outbox = Outbox(str(tmp_path / "outbox-dup.db"))
    event = _make_event("evt-dup")
    _put(outbox, event)

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
def test_consumer_dedup_survives_restart(tmp_path) -> None:
    """消费者去重记录持久化：重启后重新投递不产生重复副作用。"""
    path = str(tmp_path / "consumer.db")
    event = _make_event("evt-restart")
    side_effects: list[str] = []

    def handler(ev: OutboxEvent) -> None:
        side_effects.append(ev.event_id)

    consumer_before = IdempotentConsumer("billing", path)
    assert consumer_before.consume(event, handler) is True  # 重启前已处理过一次
    consumer_before.close()

    # 模拟进程重启：新实例从数据库读取去重记录。
    consumer_after = IdempotentConsumer("billing", path)
    try:
        assert consumer_after.consume(event, handler) is False
        assert consumer_after.is_processed("evt-restart") is True
    finally:
        consumer_after.close()
    assert side_effects == ["evt-restart"]


# ---------------------------------------------------------------------- #
# 语义四：退避重试、死信与重新投递
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_publish_failure_is_retried_with_backoff_until_delivered(tmp_path) -> None:
    """注入发布失败：事件按指数退避重试，发布目标恢复后最终送达。"""
    outbox = Outbox(str(tmp_path / "outbox-retry.db"))
    _put(outbox, _make_event("evt-retry"))
    start = 2e9
    now = [start]
    attempts: list[float] = []

    def flaky_publisher(ev: OutboxEvent) -> None:
        attempts.append(now[0] - start)
        if len(attempts) < 3:
            raise ConnectionError("broker unreachable")

    dispatcher = Dispatcher(outbox, flaky_publisher, clock=lambda: now[0])
    for _ in range(8):  # 每 0.5 秒一轮
        _run(dispatcher.dispatch_once())
        now[0] += 0.5

    # 两次失败后分别等待 1 秒、2 秒：重试发生在 +1 秒与 +3 秒，第三次送达。
    assert attempts == [0.0, 1.0, 3.0]
    assert outbox.get_stats() == {"published": 1}


@pytest.mark.contract
def test_event_is_dead_after_the_retry_limit(tmp_path, caplog) -> None:
    """失败次数达到上限：进入 dead，可查询（含次数与原因）、记录错误日志，不再自动重试。"""
    outbox = Outbox(str(tmp_path / "outbox-dead.db"))
    _put(outbox, _make_event("evt-dead"))
    now = [2e9]
    attempts: list[str] = []

    def broken_publisher(ev: OutboxEvent) -> None:
        attempts.append(ev.event_id)
        raise RuntimeError("broker unreachable")

    dispatcher = Dispatcher(
        outbox, broken_publisher, retry=RetryPolicy(max_attempts=3), clock=lambda: now[0]
    )
    for _ in range(6):  # 每轮都已过退避时间
        assert _run(dispatcher.dispatch_once()) == 0
        now[0] += 3600

    assert attempts == ["evt-dead"] * 3
    assert outbox.get_stats() == {"dead": 1}
    assert outbox.get_pending() == []
    [dead] = outbox.list_dead()
    assert (dead.event_id, dead.attempts) == ("evt-dead", 3)
    assert dead.last_error == "RuntimeError: broker unreachable"
    assert "evt-dead (Created) is dead after 3 failed attempts" in caplog.text


@pytest.mark.contract
def test_restart_does_not_redeliver_published_events(tmp_path) -> None:
    """进程重启：已发布的事件不再发布，只发布重启前尚未发布的事件。"""
    path = str(tmp_path / "outbox-restart.db")
    deliveries: list[str] = []

    def publisher(ev: OutboxEvent) -> None:
        deliveries.append(ev.event_id)

    outbox = Outbox(path)
    _put(outbox, _make_event("evt-before"))
    assert _run(Dispatcher(outbox, publisher).dispatch_once()) == 1
    _put(outbox, _make_event("evt-after"))
    _run(outbox.close())

    restarted = Outbox(path)
    try:
        assert _run(Dispatcher(restarted, publisher).dispatch_once()) == 1
        assert _run(Dispatcher(restarted, publisher).dispatch_once()) == 0
    finally:
        _run(restarted.close())
    assert deliveries == ["evt-before", "evt-after"]


@pytest.mark.contract
def test_redrive_dead_event(tmp_path) -> None:
    """重新投递：dead 事件经 ``redrive`` 放回队列后成功发布，副作用恰好一次。"""
    outbox = Outbox(str(tmp_path / "outbox-replay.db"))
    _put(outbox, _make_event("evt-replay"))

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

    # 首轮发布失败且不再重试：进入 dead，未产生副作用。
    failed_round = Dispatcher(outbox, flaky_publisher, retry=RetryPolicy(max_attempts=1))
    assert _run(failed_round.dispatch_once()) == 0
    assert outbox.get_stats() == {"dead": 1}
    assert side_effects == []

    # 运维重新投递。
    assert outbox.redrive(["evt-replay"]) == 1
    assert [e.event_id for e in outbox.get_pending()] == ["evt-replay"]

    replay_round = Dispatcher(outbox, flaky_publisher)
    count = _run(replay_round.dispatch_once())

    # 重放成功：事件被重新发布并标记 published，副作用恰好一次。
    assert count == 1
    assert deliveries == ["evt-replay", "evt-replay"]  # 失败一次 + 重放一次
    assert side_effects == ["evt-replay"]
    assert outbox.get_stats() == {"published": 1}
