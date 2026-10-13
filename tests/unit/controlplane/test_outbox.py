"""``forgemesh.controlplane.outbox`` 单元测试。

覆盖事务性 Outbox 与 Dispatcher 的核心语义：

* 事件写入与查询（put / get_pending）；
* 标记已发布（mark_published）与重复标记抛错；
* 标记失败（mark_failed）；
* 统计信息（get_stats）；
* Dispatcher 单轮分发（dispatch_once）；
* Dispatcher 幂等（重复 dispatch 不重复处理）；
* Dispatcher 异步 publisher 回调；
* 幂等消费者首次消费 / 重复消费 / 计数。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""
from __future__ import annotations

import asyncio

import pytest
from forgemesh.controlplane.outbox import (
    Dispatcher,
    IdempotentConsumer,
    Outbox,
    OutboxEvent,
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
    """标记失败。"""
    outbox = Outbox()
    _put(outbox, _make_event())

    outbox.mark_failed("evt-1", reason="connection refused")

    pending = outbox.get_pending()
    assert len(pending) == 0
    stats = outbox.get_stats()
    assert stats.get("failed") == 1


@pytest.mark.unit
def test_get_stats() -> None:
    """统计各状态数量。"""
    outbox = Outbox()
    _put(outbox, _make_event("evt-1"))
    _put(outbox, _make_event("evt-2"))
    _put(outbox, _make_event("evt-3"))

    outbox.mark_published("evt-1")
    outbox.mark_failed("evt-2")

    stats = outbox.get_stats()
    assert stats.get("pending") == 1
    assert stats.get("published") == 1
    assert stats.get("failed") == 1


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
