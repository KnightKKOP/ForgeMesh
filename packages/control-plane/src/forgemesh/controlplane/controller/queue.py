"""按键去重的延迟工作队列。

语义参照 client-go ``workqueue``：

* :meth:`WorkQueue.add` 对同一键去重；键正在处理时再次加入，会在
  :meth:`WorkQueue.done` 之后重新排队，因此同一键不会被两个 worker 同时处理；
* :meth:`WorkQueue.add_after` 延迟加入，同一键只保留最早的到期时间；
* :meth:`WorkQueue.add_rate_limited` 按该键的连续失败次数指数退避（有上限、
  带随机抖动），:meth:`WorkQueue.forget` 清零；
* :meth:`WorkQueue.shutdown` 之后 :meth:`WorkQueue.get` 立即返回 ``None``，
  尚未处理的键被丢弃（控制器下次启动时全量重建队列）。

只能在单个事件循环中使用，不是线程安全的。
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import random
import time
from collections import deque
from collections.abc import Hashable


class WorkQueue[K: Hashable]:
    """按键去重的延迟工作队列。

    Args:
        base_delay: 第一次失败后的退避秒数。
        max_delay: 退避上限（秒）。
        jitter: 随机抖动比例，实际退避落在 ``[delay * (1 - jitter), delay]``。
        rng: 随机数源（测试可注入以固定抖动）。
    """

    def __init__(
        self,
        *,
        base_delay: float = 0.5,
        max_delay: float = 300.0,
        jitter: float = 0.1,
        rng: random.Random | None = None,
    ) -> None:
        if base_delay <= 0 or max_delay < base_delay:
            raise ValueError("require 0 < base_delay <= max_delay")
        if not 0 <= jitter < 1:
            raise ValueError("jitter must be in [0, 1)")
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._jitter = jitter
        self._rng = rng or random.Random()
        self._ready: deque[K] = deque()
        self._dirty: set[K] = set()
        self._processing: set[K] = set()
        self._waiting: list[tuple[float, int, K]] = []
        self._due: dict[K, float] = {}
        self._sequence = itertools.count()
        self._failures: dict[K, int] = {}
        self._wakeup = asyncio.Event()
        self._shutting_down = False

    def __len__(self) -> int:
        """可立即取出的键数量（不含延迟中的键）。"""
        return len(self._ready)

    def add(self, key: K) -> None:
        """加入键；已在队列中则忽略，正在处理则在 :meth:`done` 后重新排队。"""
        if self._shutting_down or key in self._dirty:
            return
        self._dirty.add(key)
        if key not in self._processing:
            self._ready.append(key)
            self._wakeup.set()

    def add_after(self, key: K, delay: float) -> None:
        """``delay`` 秒后加入键；同一键已有更早的到期时间时保留更早的。"""
        if self._shutting_down:
            return
        if delay <= 0:
            self.add(key)
            return
        due = time.monotonic() + delay
        current = self._due.get(key)
        if current is not None and current <= due:
            return
        self._due[key] = due
        heapq.heappush(self._waiting, (due, next(self._sequence), key))
        self._wakeup.set()

    def add_rate_limited(self, key: K) -> float:
        """按连续失败次数退避后加入键，返回本次退避秒数。"""
        failures = self._failures.get(key, 0)
        self._failures[key] = failures + 1
        delay = min(self._max_delay, self._base_delay * 2 ** min(failures, 32))
        delay *= 1 - self._jitter * self._rng.random()
        self.add_after(key, delay)
        return delay

    def forget(self, key: K) -> None:
        """清零键的连续失败次数。"""
        self._failures.pop(key, None)

    def failures(self, key: K) -> int:
        """键的连续失败次数。"""
        return self._failures.get(key, 0)

    async def get(self) -> K | None:
        """取出下一个键并标记为处理中；队列关闭后返回 ``None``。"""
        while True:
            if self._shutting_down:
                return None
            self._promote_due()
            if self._ready:
                key = self._ready.popleft()
                self._dirty.discard(key)
                self._processing.add(key)
                return key
            self._wakeup.clear()
            timeout = self._waiting[0][0] - time.monotonic() if self._waiting else None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout)

    def done(self, key: K) -> None:
        """标记键处理完毕；处理期间再次加入过的键重新排队。"""
        self._processing.discard(key)
        if key in self._dirty and not self._shutting_down:
            self._ready.append(key)
            self._wakeup.set()

    def shutdown(self) -> None:
        """关闭队列：之后的加入被忽略，等待中的 :meth:`get` 返回 ``None``。"""
        self._shutting_down = True
        self._wakeup.set()

    def _promote_due(self) -> None:
        now = time.monotonic()
        while self._waiting and self._waiting[0][0] <= now:
            due, _, key = heapq.heappop(self._waiting)
            if self._due.get(key) == due:
                del self._due[key]
                self.add(key)
