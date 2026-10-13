"""不依赖第三方库的 Prometheus 指标：计数器、仪表盘与直方图，按文本格式 0.0.4 输出。

* 指标名与标签名在创建时校验；每个指标的标签名固定，样本按标签取值区分序列，输出时
  序列按标签取值排序。
* 标签基数有上限：调用方只传取值有限的标签（路由模板、枚举、固定字符串），不传 uid、
  名称、租户等。作为兜底，一个指标最多 ``max_series`` 个序列，之后出现的新标签组合计入
  全部标签都取 :data:`OVERFLOW_LABEL_VALUE` 的溢出序列（总数仍然准确），并记录一次告警。
* 线程安全：更新与读取都持锁（Registry 的数据库操作在工作线程中执行）。
* 仪表盘反映抓取时的状态：由 :meth:`MetricsRegistry.add_collector` 登记的采集器在每次
  :meth:`MetricsRegistry.scrape` 时以 :meth:`Gauge.replace` 整体替换取值；采集器失败时
  记录告警，并清空它负责的仪表盘，不输出过时的取值。
"""

from __future__ import annotations

import bisect
import itertools
import logging
import math
import re
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import ClassVar

logger = logging.getLogger(__name__)

#: ``/metrics`` 响应的 Content-Type（Prometheus 文本格式 0.0.4）。
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: 默认的直方图桶上界（秒），与 Prometheus 客户端库的默认值相同。
DEFAULT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

#: 每个指标默认最多的序列数。
DEFAULT_MAX_SERIES = 200

#: 溢出序列的标签取值。
OVERFLOW_LABEL_VALUE = "overflow"

_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
#: 直方图的 ``le`` 与 ``_bucket`` / ``_sum`` / ``_count`` 后缀由本模块生成，不能再用作标签名。
_RESERVED_LABELS = frozenset({"le", "quantile"})

Collector = Callable[[], Awaitable[None]]


def format_value(value: float) -> str:
    """把样本值写成文本格式：整数不带小数点，特殊值为 ``+Inf`` / ``-Inf`` / ``NaN``。"""
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _labels_text(names: Sequence[str], values: Sequence[str]) -> str:
    if not names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label_value(value)}"' for name, value in zip(names, values, strict=True)
    )
    return "{" + pairs + "}"


class _Metric:
    """单个指标：名称、说明、固定的标签名与按标签取值区分的序列。"""

    kind: ClassVar[str]

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"invalid metric name {name!r}")
        labelnames = tuple(labelnames)
        for label in labelnames:
            if not _LABEL_NAME.fullmatch(label) or label.startswith("__"):
                raise ValueError(f"invalid label name {label!r} for metric {name}")
            if label in _RESERVED_LABELS:
                raise ValueError(f"label name {label!r} is reserved (metric {name})")
        if len(set(labelnames)) != len(labelnames):
            raise ValueError(f"duplicate label names for metric {name}: {labelnames}")
        if max_series < 1:
            raise ValueError(f"max_series must be >= 1, got {max_series}")
        self.name = name
        self.documentation = documentation
        self.labelnames = labelnames
        self._max_series = max_series
        self._lock = threading.Lock()
        self._overflow_logged = False

    def _check(self, labelvalues: Sequence[str]) -> tuple[str, ...]:
        if len(labelvalues) != len(self.labelnames):
            raise ValueError(
                f"metric {self.name} expects labels {self.labelnames}, got {tuple(labelvalues)}"
            )
        for value in labelvalues:
            if not isinstance(value, str):
                raise TypeError(f"label values of metric {self.name} must be str, got {value!r}")
        return tuple(labelvalues)

    def _bounded(
        self, series: Mapping[tuple[str, ...], object], key: tuple[str, ...]
    ) -> tuple[str, ...]:
        """``key`` 已有序列或尚未达到上限时原样返回，否则返回溢出序列的键。调用方须持锁。"""
        if key in series or len(series) < self._max_series:
            return key
        if not self._overflow_logged:
            self._overflow_logged = True
            logger.warning(
                "metric %s reached %d label sets; new label sets are counted under %r",
                self.name,
                self._max_series,
                OVERFLOW_LABEL_VALUE,
            )
        return (OVERFLOW_LABEL_VALUE,) * len(self.labelnames)

    def render(self) -> list[str]:
        """输出 ``# HELP`` / ``# TYPE`` 与全部样本行。"""
        lines = [
            f"# HELP {self.name} {_escape_help(self.documentation)}",
            f"# TYPE {self.name} {self.kind}",
        ]
        lines.extend(self._sample_lines())
        return lines

    def _sample_lines(self) -> list[str]:
        raise NotImplementedError


class _ScalarMetric(_Metric):
    """每个序列是一个数值的指标（计数器、仪表盘）。"""

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        super().__init__(name, documentation, labelnames, max_series=max_series)
        self._values: dict[tuple[str, ...], float] = {}

    def value(self, *labelvalues: str) -> float:
        """序列的当前取值；序列不存在时为 0。"""
        key = self._check(labelvalues)
        with self._lock:
            return self._values.get(key, 0)

    def _sample_lines(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [
            f"{self.name}{_labels_text(self.labelnames, key)} {format_value(value)}"
            for key, value in items
        ]


class Counter(_ScalarMetric):
    """只增不减的计数器（名称按约定以 ``_total`` 结尾）。"""

    kind = "counter"

    def inc(self, *labelvalues: str, amount: float = 1) -> None:
        """序列加 ``amount``（不能为负）。"""
        if amount < 0:
            raise ValueError(f"counter {self.name} cannot decrease (amount={amount})")
        key = self._check(labelvalues)
        with self._lock:
            key = self._bounded(self._values, key)
            self._values[key] = self._values.get(key, 0) + amount


class Gauge(_ScalarMetric):
    """可增可减的仪表盘。"""

    kind = "gauge"

    def set(self, value: float, *labelvalues: str) -> None:
        """设置序列的取值。"""
        key = self._check(labelvalues)
        with self._lock:
            key = self._bounded(self._values, key)
            self._values[key] = value

    def replace(self, values: Mapping[tuple[str, ...], float]) -> None:
        """以 ``values``（标签取值元组 → 取值）整体替换全部序列；溢出的序列取值相加。"""
        checked = [(self._check(key), value) for key, value in values.items()]
        fresh: dict[tuple[str, ...], float] = {}
        with self._lock:
            for key, value in checked:
                bounded = self._bounded(fresh, key)
                fresh[bounded] = fresh.get(bounded, 0) + value
            self._values = fresh


class _HistogramSeries:
    __slots__ = ("buckets", "count", "sum")

    def __init__(self, size: int) -> None:
        self.buckets = [0] * size
        self.count = 0
        self.sum: float = 0


class Histogram(_Metric):
    """直方图：按桶上界累计观测次数，并记录总和与总次数。

    Args:
        buckets: 严格递增的有限桶上界；``+Inf`` 桶自动追加。
    """

    kind = "histogram"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> None:
        super().__init__(name, documentation, labelnames, max_series=max_series)
        bounds = tuple(float(bound) for bound in buckets)
        if not bounds:
            raise ValueError(f"histogram {name} needs at least one bucket")
        if any(not math.isfinite(bound) for bound in bounds):
            raise ValueError(f"histogram {name} buckets must be finite (+Inf is implicit)")
        if any(low >= high for low, high in itertools.pairwise(bounds)):
            raise ValueError(f"histogram {name} buckets must be strictly increasing: {bounds}")
        self.buckets = bounds
        self._series: dict[tuple[str, ...], _HistogramSeries] = {}

    def observe(self, value: float, *labelvalues: str) -> None:
        """记录一次观测。"""
        if math.isnan(value):
            raise ValueError(f"histogram {self.name} cannot observe NaN")
        key = self._check(labelvalues)
        index = bisect.bisect_left(self.buckets, value)
        with self._lock:
            key = self._bounded(self._series, key)
            series = self._series.get(key)
            if series is None:
                series = self._series[key] = _HistogramSeries(len(self.buckets) + 1)
            series.buckets[index] += 1
            series.count += 1
            series.sum += value

    def count(self, *labelvalues: str) -> int:
        """序列的观测次数；序列不存在时为 0。"""
        key = self._check(labelvalues)
        with self._lock:
            series = self._series.get(key)
            return series.count if series is not None else 0

    def _sample_lines(self) -> list[str]:
        with self._lock:
            items = sorted(
                (key, list(series.buckets), series.sum, series.count)
                for key, series in self._series.items()
            )
        bucket_labels = (*self.labelnames, "le")
        lines: list[str] = []
        for key, buckets, total, count in items:
            cumulative = 0
            for bound, observed in zip((*self.buckets, math.inf), buckets, strict=True):
                cumulative += observed
                labels = _labels_text(bucket_labels, (*key, format_value(bound)))
                lines.append(f"{self.name}_bucket{labels} {cumulative}")
            labels = _labels_text(self.labelnames, key)
            lines.append(f"{self.name}_sum{labels} {format_value(total)}")
            lines.append(f"{self.name}_count{labels} {count}")
        return lines


class MetricsRegistry:
    """一组指标：按创建顺序输出，抓取前执行登记的采集器。"""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._collectors: list[tuple[Collector, tuple[Gauge, ...]]] = []
        self._lock = threading.Lock()

    def _register[M: _Metric](self, metric: M) -> M:
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"metric {metric.name} is already registered")
            self._metrics[metric.name] = metric
        return metric

    def counter(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> Counter:
        """创建并登记一个计数器。"""
        return self._register(Counter(name, documentation, labelnames, max_series=max_series))

    def gauge(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> Gauge:
        """创建并登记一个仪表盘。"""
        return self._register(Gauge(name, documentation, labelnames, max_series=max_series))

    def histogram(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        max_series: int = DEFAULT_MAX_SERIES,
    ) -> Histogram:
        """创建并登记一个直方图。"""
        return self._register(
            Histogram(name, documentation, labelnames, buckets=buckets, max_series=max_series)
        )

    def add_collector(self, collector: Collector, gauges: Iterable[Gauge]) -> Callable[[], None]:
        """登记抓取时执行的采集器，返回注销函数。

        采集器负责替换 ``gauges`` 的取值；它抛出异常时记录告警并清空这些仪表盘。
        """
        entry = (collector, tuple(gauges))
        with self._lock:
            self._collectors.append(entry)

        def remove() -> None:
            with self._lock:
                if entry in self._collectors:
                    self._collectors.remove(entry)

        return remove

    def render(self) -> str:
        """按文本格式输出全部指标的当前取值（不执行采集器）。"""
        with self._lock:
            metrics = list(self._metrics.values())
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"

    async def scrape(self) -> str:
        """依次执行采集器，再输出全部指标。"""
        with self._lock:
            collectors = list(self._collectors)
        for collector, gauges in collectors:
            try:
                await collector()
            except Exception:  # noqa: BLE001 - 一个采集器失败不影响其他指标
                logger.warning("metrics collector %r failed", collector, exc_info=True)
                for gauge in gauges:
                    gauge.replace({})
        return self.render()


__all__ = [
    "CONTENT_TYPE",
    "DEFAULT_BUCKETS",
    "DEFAULT_MAX_SERIES",
    "OVERFLOW_LABEL_VALUE",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "format_value",
]
