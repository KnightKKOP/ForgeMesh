"""``forgemesh.controlplane.outbox`` 单元测试。

覆盖事务性 Outbox 与 Dispatcher 的核心语义：

* 事件写入与查询（put / get / get_pending）；
* 标记已发布（mark_published）与重复标记抛错；
* 发布失败：指数退避（RetryPolicy）、到期查询（due）、达到上限进入 dead、
  重新投递（redrive）、错误截断；
* 统计信息（get_stats、backlog）与已发布事件清理（purge_published）；
* 存量库迁移（补列、旧 failed 改为 dead）；
* Dispatcher 单轮分发（dispatch_once）、遇到失败结束本轮；
* Dispatcher 在发布尝试的结果写入数据库之后才报告它（on_attempt）；
* Dispatcher 幂等（重复 dispatch 不重复处理）；
* Dispatcher 异步 publisher 回调；
* Dispatcher 后台运行：持续发布、数据库错误后继续、停止与超时取消；
* 幂等消费者首次消费 / 重复消费 / 计数、handler 失败不记录、按名称去重、
  并发投递只处理一次。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from forgemesh.controlplane.outbox import (
    Dispatcher,
    IdempotentConsumer,
    Outbox,
    OutboxBacklog,
    OutboxEvent,
    OutboxStatus,
    PublishOutcome,
    RetryPolicy,
)


def _run(coro):
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
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


def _put(outbox: Outbox, *events: OutboxEvent) -> None:
    """在一个事务中写入事件（put 不自行提交）。"""
    with outbox.database.transaction():
        for event in events:
            outbox.put(event)


# ---------------------------------------------------------------------- #
# Outbox 基本操作
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_put_requires_the_callers_transaction() -> None:
    """put 不自行提交：事务之外调用被拒绝，不写入事件。"""
    outbox = Outbox()
    with pytest.raises(RuntimeError, match="inside the caller's transaction"):
        outbox.put(_make_event())
    assert outbox.get_stats() == {}


@pytest.mark.unit
def test_put_and_get_pending() -> None:
    """写入事件后可查到 pending。"""
    outbox = Outbox()
    event = _make_event()
    _put(outbox, event)

    pending = outbox.get_pending()
    assert len(pending) == 1
    assert pending[0].event_id == "evt-1"
    assert pending[0].status == "pending"
    assert pending[0].event_type == "Created"
    assert pending[0].payload == {"name": "demo"}


@pytest.mark.unit
def test_get_returns_an_event_in_any_state() -> None:
    """get 按 event_id 取事件，不论状态；不存在时返回 None。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"), _make_event("evt-2"))
    outbox.mark_published("evt-1", now=100.0)

    published, pending = outbox.get("evt-1"), outbox.get("evt-2")
    assert (published.status, published.published_at) == (OutboxStatus.PUBLISHED, 100.0)
    assert (pending.status, pending.payload) == (OutboxStatus.PENDING, {"name": "demo"})
    assert outbox.get("missing") is None


@pytest.mark.unit
def test_mark_published() -> None:
    """标记后 status 变为 published。"""
    outbox = Outbox()
    _put(outbox, _make_event())

    outbox.mark_published("evt-1")

    pending = outbox.get_pending()
    assert len(pending) == 0
    stats = outbox.get_stats()
    assert stats.get("published") == 1


@pytest.mark.unit
def test_mark_published_already_published() -> None:
    """重复标记抛 ValueError。"""
    outbox = Outbox()
    _put(outbox, _make_event())
    outbox.mark_published("evt-1")

    with pytest.raises(ValueError, match="already published"):
        outbox.mark_published("evt-1")


@pytest.mark.unit
def test_mark_failed() -> None:
    """一次失败：仍为 pending，记录失败次数与原因，退避期内不到期。"""
    outbox = Outbox()
    _put(outbox, _make_event())

    failed = outbox.mark_failed("evt-1", reason="connection refused", now=100.0)

    assert failed.status == OutboxStatus.PENDING
    assert (failed.attempts, failed.next_attempt_at) == (1, 101.0)
    assert failed.last_error == "connection refused"
    [stored] = outbox.get_pending()
    assert stored == failed
    assert outbox.due(now=100.5) == []
    assert [e.event_id for e in outbox.due(now=101.0)] == ["evt-1"]


@pytest.mark.unit
def test_get_stats() -> None:
    """统计各状态数量。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"))
    _put(outbox, _make_event("evt-2"))
    _put(outbox, _make_event("evt-3"))

    outbox.mark_published("evt-1")
    outbox.mark_failed("evt-2", policy=RetryPolicy(max_attempts=1))

    stats = outbox.get_stats()
    assert stats.get("pending") == 1
    assert stats.get("published") == 1
    assert stats.get("dead") == 1


@pytest.mark.unit
def test_backlog_splits_pending_events_by_past_failures_and_counts_dead_ones() -> None:
    outbox = Outbox()
    assert outbox.backlog() == OutboxBacklog(pending=0, retrying=0, dead=0)
    _put(outbox, *(_make_event(f"evt-{n}") for n in range(1, 6)))
    outbox.mark_published("evt-1")
    outbox.mark_failed("evt-2", "boom", now=10.0)
    outbox.mark_failed("evt-3", "boom", policy=RetryPolicy(max_attempts=1))

    assert outbox.backlog() == OutboxBacklog(pending=2, retrying=1, dead=1)
    assert outbox.redrive(["evt-3"], now=20.0) == 1
    assert outbox.backlog() == OutboxBacklog(pending=3, retrying=1, dead=0)


@pytest.mark.unit
def test_retry_delay_grows_exponentially_and_is_capped() -> None:
    default = RetryPolicy()
    assert default.max_attempts == 10
    assert [default.delay(n) for n in range(1, 10)] == [1, 2, 4, 8, 16, 32, 64, 128, 256]
    capped = RetryPolicy(max_attempts=100, base_delay=0.5, max_delay=3.0)
    assert [capped.delay(n) for n in (1, 2, 3, 4, 99)] == [0.5, 1.0, 2.0, 3.0, 3.0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"base_delay": 0}, {"base_delay": 2.0, "max_delay": 1.0}],
)
def test_retry_policy_rejects_invalid_settings(kwargs) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


@pytest.mark.unit
def test_mark_failed_backs_off_then_moves_the_event_to_dead() -> None:
    outbox = Outbox()
    _put(outbox, _make_event())
    policy = RetryPolicy(max_attempts=3, base_delay=1.0)

    first = outbox.mark_failed("evt-1", "e1", policy=policy, now=10.0)
    second = outbox.mark_failed("evt-1", "e2", policy=policy, now=11.0)
    dead = outbox.mark_failed("evt-1", "e3", policy=policy, now=13.0)

    assert (first.attempts, first.next_attempt_at) == (1, 11.0)
    assert (second.attempts, second.next_attempt_at) == (2, 13.0)
    assert (dead.status, dead.attempts, dead.next_attempt_at) == (OutboxStatus.DEAD, 3, None)
    assert outbox.due(now=1e12) == []
    assert outbox.list_dead() == [dead]
    assert dead.last_error == "e3"
    with pytest.raises(ValueError, match="not pending"):
        outbox.mark_failed("evt-1", policy=policy)


@pytest.mark.unit
def test_mark_failed_requires_a_pending_event() -> None:
    outbox = Outbox()
    _put(outbox, _make_event())
    outbox.mark_published("evt-1")
    with pytest.raises(ValueError, match="not pending"):
        outbox.mark_failed("evt-1")
    with pytest.raises(ValueError, match="not pending"):
        outbox.mark_failed("missing")


@pytest.mark.unit
def test_last_error_is_truncated() -> None:
    outbox = Outbox()
    _put(outbox, _make_event())
    failed = outbox.mark_failed("evt-1", "x" * 2000)
    assert failed.last_error == "x" * 500
    assert outbox.get_pending()[0].last_error == "x" * 500


@pytest.mark.unit
def test_due_returns_events_in_creation_order_and_skips_backoff() -> None:
    outbox = Outbox()
    _put(
        outbox,
        _make_event("evt-b").model_copy(update={"created_at": 2.0}),
        _make_event("evt-a").model_copy(update={"created_at": 1.0}),
        _make_event("evt-c").model_copy(update={"created_at": 3.0}),
    )
    assert [e.event_id for e in outbox.due(now=5.0)] == ["evt-a", "evt-b", "evt-c"]
    assert [e.event_id for e in outbox.due(limit=1, now=5.0)] == ["evt-a"]
    assert [e.event_id for e in outbox.due(now=2.5)] == ["evt-a", "evt-b"]

    outbox.mark_failed("evt-a", now=5.0)
    assert [e.event_id for e in outbox.due(now=5.0)] == ["evt-b", "evt-c"]
    assert [e.event_id for e in outbox.due(now=6.0)] == ["evt-a", "evt-b", "evt-c"]


@pytest.mark.unit
def test_redrive_requeues_dead_events_only() -> None:
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"), _make_event("evt-2"), _make_event("evt-3"))
    kill = RetryPolicy(max_attempts=1)
    outbox.mark_failed("evt-1", "boom", policy=kill)
    outbox.mark_failed("evt-2", "boom", policy=kill)

    assert outbox.redrive([], now=50.0) == 0
    assert outbox.redrive(["evt-1", "evt-1", "evt-3", "missing"], now=50.0) == 1
    redriven = next(e for e in outbox.get_pending() if e.event_id == "evt-1")
    assert (redriven.attempts, redriven.next_attempt_at, redriven.last_error) == (0, 50.0, None)
    assert [e.event_id for e in outbox.list_dead()] == ["evt-2"]

    assert outbox.redrive(now=50.0) == 1
    assert outbox.list_dead() == []
    assert outbox.get_stats() == {"pending": 3}


@pytest.mark.unit
def test_purge_published_keeps_recent_and_unpublished_events() -> None:
    outbox = Outbox()
    _put(outbox, _make_event("old"), _make_event("new"), _make_event("waiting"))
    outbox.mark_published("old", now=10.0)
    outbox.mark_published("new", now=30.0)

    assert outbox.purge_published(before=20.0) == 1
    assert outbox.get_stats() == {"pending": 1, "published": 1}


@pytest.mark.unit
def test_legacy_outbox_table_is_migrated(tmp_path) -> None:
    """v1.0.0 的表：补齐重试列，旧的 failed 改为可重新投递的 dead，pending 立即到期。"""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE outbox_events (
            event_id TEXT PRIMARY KEY,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL,
            published_at REAL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        INSERT INTO outbox_events VALUES ('p', 'T', 'a', 't', '{}', 1.0, NULL, 'pending');
        INSERT INTO outbox_events VALUES ('f', 'T', 'a', 't', '{}', 2.0, NULL, 'failed');
        INSERT INTO outbox_events VALUES ('d', 'T', 'a', 't', '{}', 3.0, 4.0, 'published');
        """
    )
    conn.close()

    outbox = Outbox(path)
    try:
        assert outbox.get_stats() == {"pending": 1, "dead": 1, "published": 1}
        [pending] = outbox.due(now=1.0)
        assert (pending.event_id, pending.attempts, pending.next_attempt_at) == ("p", 0, 1.0)
        assert [e.event_id for e in outbox.list_dead()] == ["f"]
        assert outbox.redrive(["f"]) == 1
    finally:
        _run(outbox.close())

    reopened = Outbox(path)
    try:
        assert reopened.get_stats() == {"pending": 2, "published": 1}
    finally:
        _run(reopened.close())


# ---------------------------------------------------------------------- #
# Dispatcher
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_dispatcher_dispatch_once() -> None:
    """Dispatcher 发布事件并标记。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"))
    _put(outbox, _make_event("evt-2"))

    published: list[str] = []

    def publisher(event: OutboxEvent) -> None:
        published.append(event.event_id)

    dispatcher = Dispatcher(outbox, publisher)
    count = _run(dispatcher.dispatch_once())

    assert count == 2
    assert set(published) == {"evt-1", "evt-2"}
    assert outbox.get_stats().get("published") == 2


@pytest.mark.unit
def test_dispatcher_idempotent() -> None:
    """重复 dispatch 不重复处理。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"))

    call_count = 0

    def publisher(event: OutboxEvent) -> None:
        nonlocal call_count
        call_count += 1

    dispatcher = Dispatcher(outbox, publisher)
    first = _run(dispatcher.dispatch_once())
    second = _run(dispatcher.dispatch_once())

    assert first == 1
    assert second == 0  # 已发布，无 pending 事件
    assert call_count == 1  # publisher 只调用一次


@pytest.mark.unit
def test_dispatcher_with_async_publisher() -> None:
    """异步 publisher 回调。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"))
    _put(outbox, _make_event("evt-2"))

    published: list[str] = []

    async def async_publisher(event: OutboxEvent) -> None:
        await asyncio.sleep(0)  # 模拟异步操作
        published.append(event.event_id)

    dispatcher = Dispatcher(outbox, async_publisher)
    count = _run(dispatcher.dispatch_once())

    assert count == 2
    assert set(published) == {"evt-1", "evt-2"}
    assert outbox.get_stats().get("published") == 2


@pytest.mark.unit
def test_dispatcher_ends_the_round_at_the_first_failure() -> None:
    """发布失败即结束本轮；失败的事件退避期间，后面的事件在下一轮照常发布。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"), _make_event("evt-2"), _make_event("evt-3"))
    calls: list[str] = []

    def publisher(event: OutboxEvent) -> None:
        calls.append(event.event_id)
        if event.event_id == "evt-1":
            raise ConnectionError("refused")

    dispatcher = Dispatcher(outbox, publisher, clock=lambda: 2e9)
    assert _run(dispatcher.dispatch_once()) == 0
    assert calls == ["evt-1"]
    assert _run(dispatcher.dispatch_once()) == 2
    assert calls == ["evt-1", "evt-2", "evt-3"]
    [failed] = outbox.get_pending()
    assert (failed.event_id, failed.attempts) == ("evt-1", 1)
    assert failed.last_error == "ConnectionError: refused"


@pytest.mark.unit
def test_dispatcher_reports_each_attempt_after_recording_its_outcome() -> None:
    """on_attempt 收到每次尝试的结果时，结果已经写入数据库。"""
    outbox = Outbox()
    _put(
        outbox,
        _make_event("evt-ok").model_copy(update={"created_at": 1.0}),
        _make_event("evt-bad").model_copy(update={"created_at": 2.0}),
    )
    now = 100.0
    seen: list[tuple[PublishOutcome, OutboxBacklog]] = []

    def publisher(event: OutboxEvent) -> None:
        if event.event_id == "evt-bad":
            raise ConnectionError("refused")

    dispatcher = Dispatcher(
        outbox,
        publisher,
        retry=RetryPolicy(max_attempts=2, base_delay=1.0),
        clock=lambda: now,
        on_attempt=lambda outcome: seen.append((outcome, outbox.backlog())),
    )
    assert _run(dispatcher.dispatch_once()) == 1
    now = 101.0
    assert _run(dispatcher.dispatch_once()) == 0

    assert seen == [
        (PublishOutcome.PUBLISHED, OutboxBacklog(pending=1, retrying=0, dead=0)),
        (PublishOutcome.FAILED, OutboxBacklog(pending=0, retrying=1, dead=0)),
        (PublishOutcome.DEAD, OutboxBacklog(pending=0, retrying=0, dead=1)),
    ]


@pytest.mark.unit
def test_dispatcher_does_not_report_an_attempt_whose_outcome_was_not_recorded(
    monkeypatch,
) -> None:
    """发布成功但未能标记的事件不报告；它仍待发布，再次发布并标记之后才报告一次。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"))
    real_mark_published = outbox.mark_published
    calls = 0

    def flaky_mark_published(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_mark_published(*args, **kwargs)

    monkeypatch.setattr(outbox, "mark_published", flaky_mark_published)
    outcomes: list[PublishOutcome] = []
    dispatcher = Dispatcher(outbox, lambda event: None, on_attempt=outcomes.append)

    with pytest.raises(sqlite3.OperationalError):
        _run(dispatcher.dispatch_once())
    assert outcomes == []
    assert outbox.get("evt-1").status == OutboxStatus.PENDING
    assert _run(dispatcher.dispatch_once()) == 1
    assert outcomes == [PublishOutcome.PUBLISHED]


@pytest.mark.unit
@pytest.mark.parametrize("kwargs", [{"poll_interval": 0}, {"batch_size": 0}])
def test_dispatcher_rejects_invalid_settings(kwargs) -> None:
    with pytest.raises(ValueError):
        Dispatcher(Outbox(), lambda event: None, **kwargs)


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


@pytest.mark.unit
def test_dispatcher_runs_in_the_background_until_shutdown() -> None:
    async def scenario() -> None:
        outbox = Outbox()
        delivered: list[str] = []
        dispatcher = Dispatcher(
            outbox, lambda event: delivered.append(event.event_id), poll_interval=0.01
        )
        task = dispatcher.start()
        with pytest.raises(RuntimeError, match="already started"):
            dispatcher.start()
        _put(outbox, _make_event("evt-1"))
        await _wait_for(lambda: delivered == ["evt-1"])
        _put(outbox, _make_event("evt-2"))
        await _wait_for(lambda: delivered == ["evt-1", "evt-2"])
        await dispatcher.shutdown()
        assert task.done() and not task.cancelled()
        assert outbox.get_stats() == {"published": 2}

    _run(scenario())


@pytest.mark.unit
def test_dispatcher_keeps_running_after_a_database_error(monkeypatch, caplog) -> None:
    async def scenario() -> None:
        outbox = Outbox()
        _put(outbox, _make_event("evt-1"))
        real_due = outbox.due
        calls = 0

        def flaky_due(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_due(*args, **kwargs)

        monkeypatch.setattr(outbox, "due", flaky_due)
        delivered: list[str] = []
        dispatcher = Dispatcher(
            outbox, lambda event: delivered.append(event.event_id), poll_interval=0.01
        )
        dispatcher.start()
        await _wait_for(lambda: delivered == ["evt-1"])
        await dispatcher.shutdown()

    _run(scenario())
    assert "outbox dispatch failed" in caplog.text


@pytest.mark.unit
def test_shutdown_cancels_a_publisher_that_does_not_return() -> None:
    async def scenario() -> None:
        outbox = Outbox()
        _put(outbox, _make_event("evt-1"))
        started = asyncio.Event()

        async def stuck(event: OutboxEvent) -> None:
            started.set()
            await asyncio.Event().wait()

        dispatcher = Dispatcher(outbox, stuck, poll_interval=0.01)
        task = dispatcher.start()
        await asyncio.wait_for(started.wait(), 2)
        await dispatcher.shutdown(timeout=0.05)
        assert task.cancelled()
        # 未完成的发布不计为失败：事件仍待发布，重启后再次发布。
        [event] = outbox.get_pending()
        assert event.attempts == 0

    _run(scenario())


@pytest.mark.unit
def test_run_purges_published_events_past_the_retention(monkeypatch) -> None:
    async def scenario() -> None:
        outbox = Outbox()
        cutoffs: list[float] = []
        monkeypatch.setattr(outbox, "purge_published", lambda before: cutoffs.append(before))
        dispatcher = Dispatcher(
            outbox, lambda event: None, poll_interval=0.01, retention=60.0, clock=lambda: 1000.0
        )
        dispatcher.start()
        await _wait_for(lambda: cutoffs)
        await asyncio.sleep(0.05)
        await dispatcher.shutdown()
        assert cutoffs == [940.0]  # 每小时最多一次

    _run(scenario())


# ---------------------------------------------------------------------- #
# IdempotentConsumer
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_idempotent_consumer_first_time() -> None:
    """首次消费返回 True。"""
    consumer = IdempotentConsumer()
    event = _make_event()
    handled: list[str] = []

    def handler(ev: OutboxEvent) -> None:
        handled.append(ev.event_id)

    result = consumer.consume(event, handler)
    assert result is True
    assert handled == ["evt-1"]


@pytest.mark.unit
def test_idempotent_consumer_duplicate() -> None:
    """重复消费返回 False。"""
    consumer = IdempotentConsumer()
    event = _make_event()
    handled: list[str] = []

    def handler(ev: OutboxEvent) -> None:
        handled.append(ev.event_id)

    first = consumer.consume(event, handler)
    second = consumer.consume(event, handler)

    assert first is True
    assert second is False
    assert handled == ["evt-1"]  # handler 只执行一次


@pytest.mark.unit
def test_idempotent_consumer_count() -> None:
    """processed_count 正确。"""
    consumer = IdempotentConsumer()

    def handler(ev: OutboxEvent) -> None:
        pass

    consumer.consume(_make_event("evt-1"), handler)
    consumer.consume(_make_event("evt-2"), handler)
    consumer.consume(_make_event("evt-3"), handler)
    # 重复消费不计数
    consumer.consume(_make_event("evt-1"), handler)

    assert consumer.processed_count == 3
    assert consumer.is_processed("evt-1") is True
    assert consumer.is_processed("evt-4") is False


@pytest.mark.unit
def test_idempotent_consumer_does_not_record_a_failed_handler() -> None:
    """handler 抛出异常：事件不记为已处理，再次投递时重新处理。"""
    consumer = IdempotentConsumer()
    calls: list[str] = []

    def flaky(ev: OutboxEvent) -> None:
        calls.append(ev.event_id)
        if len(calls) == 1:
            raise RuntimeError("downstream unavailable")

    with pytest.raises(RuntimeError, match="downstream unavailable"):
        consumer.consume(_make_event(), flaky)
    assert consumer.is_processed("evt-1") is False
    assert consumer.consume(_make_event(), flaky) is True
    assert calls == ["evt-1", "evt-1"]


@pytest.mark.unit
def test_idempotent_consumers_dedupe_per_name_on_a_shared_database() -> None:
    """不同名称的消费者各自去重；可与 Outbox 共用一个数据库。"""
    outbox = Outbox()
    billing = IdempotentConsumer("billing", database=outbox.database)
    audit = IdempotentConsumer("audit", database=outbox.database)

    assert billing.consume(_make_event(), lambda ev: None) is True
    assert billing.consume(_make_event(), lambda ev: None) is False
    assert audit.consume(_make_event(), lambda ev: None) is True
    billing.close()  # 共用的数据库不随之关闭
    assert audit.processed_count == 1
    with pytest.raises(ValueError, match="must not be empty"):
        IdempotentConsumer("")


@pytest.mark.unit
def test_idempotent_consumer_handles_concurrent_deliveries_once() -> None:
    """同一事件被并发投递：handler 只执行一次。"""
    consumer = IdempotentConsumer()
    calls: list[str] = []

    def slow(ev: OutboxEvent) -> None:
        calls.append(ev.event_id)
        time.sleep(0.01)

    async def deliver_concurrently() -> list[bool]:
        return await asyncio.gather(
            *(asyncio.to_thread(consumer.consume, _make_event(), slow) for _ in range(8))
        )

    results = _run(deliver_concurrently())
    assert sorted(results) == [False] * 7 + [True]
    assert calls == ["evt-1"]
