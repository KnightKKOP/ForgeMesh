"""控制器工作队列单元测试：按键去重、同键串行、延迟加入、退避与关闭。"""

from __future__ import annotations

import asyncio
import random
import time

import pytest
from forgemesh.controlplane.controller import WorkQueue


def _run(coro):
    """同步驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


@pytest.mark.unit
def test_keys_are_deduplicated_and_never_handed_out_twice_at_once() -> None:
    async def scenario() -> None:
        queue: WorkQueue[str] = WorkQueue()
        queue.add("a")
        queue.add("a")
        queue.add("b")
        assert len(queue) == 2

        assert await queue.get() == "a"
        queue.add("a")
        assert await queue.get() == "b"
        assert len(queue) == 0

        queue.done("b")
        queue.done("a")
        assert await asyncio.wait_for(queue.get(), 1) == "a"

    _run(scenario())


@pytest.mark.unit
def test_add_after_delays_a_key_and_keeps_its_earliest_due_time() -> None:
    async def scenario() -> float:
        queue: WorkQueue[str] = WorkQueue()
        started = time.monotonic()
        queue.add_after("a", 60)
        queue.add_after("a", 0.05)
        queue.add_after("a", 30)
        assert len(queue) == 0

        assert await asyncio.wait_for(queue.get(), 1) == "a"
        elapsed = time.monotonic() - started
        queue.done("a")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), 0.1)
        return elapsed

    assert 0.04 <= _run(scenario()) < 1


@pytest.mark.unit
def test_rate_limited_requeue_backs_off_exponentially_up_to_the_cap() -> None:
    async def scenario() -> None:
        queue: WorkQueue[str] = WorkQueue(base_delay=1, max_delay=5, jitter=0)
        assert [queue.add_rate_limited("a") for _ in range(5)] == [1, 2, 4, 5, 5]
        assert queue.failures("a") == 5
        assert queue.failures("b") == 0

        queue.forget("a")
        assert queue.add_rate_limited("a") == 1

    _run(scenario())


@pytest.mark.unit
def test_jitter_only_shortens_the_backoff() -> None:
    async def scenario() -> list[float]:
        queue: WorkQueue[int] = WorkQueue(
            base_delay=1, max_delay=8, jitter=0.5, rng=random.Random(7)
        )
        return [queue.add_rate_limited(key) for key in range(100)]

    delays = _run(scenario())
    assert all(0.5 <= delay <= 1 for delay in delays)
    assert len(set(delays)) > 1


@pytest.mark.unit
def test_shutdown_releases_waiting_workers_and_ignores_new_keys() -> None:
    async def scenario() -> None:
        queue: WorkQueue[str] = WorkQueue()
        waiters = [asyncio.create_task(queue.get()) for _ in range(3)]
        await asyncio.sleep(0)

        queue.shutdown()

        assert await asyncio.gather(*waiters) == [None, None, None]
        queue.add("a")
        assert await queue.get() is None

    _run(scenario())


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [{"base_delay": 0}, {"base_delay": 2, "max_delay": 1}, {"jitter": 1}, {"jitter": -0.1}],
)
def test_invalid_backoff_settings_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        WorkQueue(**kwargs)
