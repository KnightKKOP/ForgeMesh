"""Prometheus 指标原语：文本格式、计数器与直方图、标签基数上限、抓取时的采集器。"""

from __future__ import annotations

import asyncio
import logging
import math
import threading

import pytest
from forgemesh.controlplane.metrics import (
    CONTENT_TYPE,
    OVERFLOW_LABEL_VALUE,
    Counter,
    Histogram,
    MetricsRegistry,
    format_value,
)

from .exposition import parse


@pytest.mark.unit
def test_counter_renders_help_type_and_sorted_series() -> None:
    registry = MetricsRegistry()
    requests = registry.counter("demo_requests_total", "Requests handled.", ("method", "code"))
    requests.inc("POST", "201")
    requests.inc("GET", "200", amount=2)
    requests.inc("GET", "200")

    text = registry.render()

    assert text == (
        "# HELP demo_requests_total Requests handled.\n"
        "# TYPE demo_requests_total counter\n"
        'demo_requests_total{method="GET",code="200"} 3\n'
        'demo_requests_total{method="POST",code="201"} 1\n'
    )
    assert parse(text)["demo_requests_total"].value(method="GET", code="200") == 3
    assert CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


@pytest.mark.unit
def test_label_values_and_help_text_are_escaped() -> None:
    registry = MetricsRegistry()
    registry.counter("demo_total", 'Back\\slash and\nnewline "kept".', ("value",)).inc('a"b\\c\nd')

    text = registry.render()

    assert '# HELP demo_total Back\\\\slash and\\nnewline "kept".\n' in text
    assert 'demo_total{value="a\\"b\\\\c\\nd"} 1\n' in text
    assert parse(text)["demo_total"].value(value='a"b\\c\nd') == 1


@pytest.mark.unit
def test_histogram_buckets_are_cumulative_with_sum_and_count() -> None:
    registry = MetricsRegistry()
    latency = registry.histogram("demo_seconds", "Latency.", ("route",), buckets=(0.1, 1, 5))
    for value in (0.05, 0.1, 0.7, 3, 60):
        latency.observe(value, "/a")

    family = parse(registry.render())["demo_seconds"]

    assert family.type == "histogram"
    buckets = {s.labels["le"]: s.value for s in family.samples if s.name == "demo_seconds_bucket"}
    # 桶上界是闭区间：0.1 计入 le="0.1"。
    assert buckets == {"0.1": 2, "1": 3, "5": 4, "+Inf": 5}
    assert family.value("demo_seconds_count", route="/a") == 5
    assert family.value("demo_seconds_sum", route="/a") == pytest.approx(63.85)
    assert latency.count("/a") == 5
    assert latency.count("/b") == 0


@pytest.mark.unit
def test_registry_output_is_valid_exposition_format() -> None:
    registry = MetricsRegistry()
    registry.counter("demo_total", "Counter.").inc()
    registry.gauge("demo_items", "Gauge.", ("state",)).set(2, "ready")
    registry.histogram("demo_seconds", "Histogram.", ("kind",)).observe(0.2, "a")
    registry.histogram("demo_empty_seconds", "Histogram without samples.")

    families = parse(registry.render())

    assert [f.type for f in families.values()] == ["counter", "gauge", "histogram", "histogram"]
    assert families["demo_total"].value() == 1
    assert families["demo_items"].value(state="ready") == 2
    assert families["demo_empty_seconds"].samples == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "text"),
    [
        (3, "3"),
        (2.0, "2"),
        (0.25, "0.25"),
        (1e-07, "1e-07"),
        (math.inf, "+Inf"),
        (-math.inf, "-Inf"),
        (math.nan, "NaN"),
    ],
)
def test_format_value(value: float, text: str) -> None:
    assert format_value(value) == text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "labels"),
    [
        ("0starts_with_digit", ()),
        ("has-dash", ()),
        ("ok_total", ("bad-label",)),
        ("ok_total", ("__reserved",)),
        ("ok_total", ("le",)),
        ("ok_total", ("dup", "dup")),
    ],
)
def test_invalid_metric_and_label_names_are_rejected(name: str, labels: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        Counter(name, "help", labels)


@pytest.mark.unit
@pytest.mark.parametrize("buckets", [(), (1, 1), (2, 1), (1, math.inf), (math.nan,)])
def test_invalid_histogram_buckets_are_rejected(buckets: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        Histogram("demo_seconds", "help", buckets=buckets)


@pytest.mark.unit
def test_label_values_must_match_the_declared_labels() -> None:
    counter = Counter("demo_total", "help", ("a", "b"))

    with pytest.raises(ValueError):
        counter.inc("only-one")
    with pytest.raises(TypeError):
        counter.inc("a", 200)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        counter.inc("a", "b", amount=-1)
    with pytest.raises(ValueError):
        Histogram("demo_seconds", "help").observe(math.nan)


@pytest.mark.unit
def test_duplicate_metric_names_are_rejected() -> None:
    registry = MetricsRegistry()
    registry.counter("demo_total", "help")

    with pytest.raises(ValueError, match="already registered"):
        registry.gauge("demo_total", "help")


@pytest.mark.unit
def test_new_label_sets_beyond_the_limit_go_to_the_overflow_series(caplog) -> None:
    registry = MetricsRegistry()
    counter = registry.counter("demo_total", "help", ("route", "code"), max_series=2)
    latency = registry.histogram("demo_seconds", "help", ("route",), max_series=1)
    caplog.set_level(logging.WARNING, logger="forgemesh.controlplane.metrics")

    for index in range(5):
        counter.inc(f"/r{index}", "200")
        latency.observe(0.01, f"/r{index}")
    counter.inc("/r0", "200")

    family = parse(registry.render())["demo_total"]
    assert {tuple(s.labels.values()): s.value for s in family.samples} == {
        ("/r0", "200"): 2,
        ("/r1", "200"): 1,
        (OVERFLOW_LABEL_VALUE, OVERFLOW_LABEL_VALUE): 3,
    }
    assert latency.count("/r0") == 1
    assert latency.count(OVERFLOW_LABEL_VALUE) == 4
    warnings = [r for r in caplog.records if "reached" in r.getMessage()]
    assert sorted(r.getMessage().split()[1] for r in warnings) == ["demo_seconds", "demo_total"]


@pytest.mark.unit
def test_gauge_replace_drops_vanished_series_and_sums_overflow() -> None:
    gauge = MetricsRegistry().gauge("demo_items", "help", ("type",), max_series=2)
    gauge.replace({("a",): 1, ("b",): 2})
    gauge.replace({("b",): 5, ("c",): 7, ("d",): 1})

    assert gauge.value("a") == 0
    assert gauge.value("b") == 5
    assert gauge.value("c") == 7
    assert gauge.value(OVERFLOW_LABEL_VALUE) == 1


@pytest.mark.unit
def test_scrape_runs_collectors_and_clears_the_gauges_of_a_failing_one(caplog) -> None:
    registry = MetricsRegistry()
    healthy = registry.gauge("demo_healthy", "help", ("state",))
    broken = registry.gauge("demo_broken", "help", ("state",))
    calls = {"count": 0}

    async def collect_healthy() -> None:
        calls["count"] += 1
        healthy.replace({("up",): calls["count"]})

    async def collect_broken() -> None:
        raise RuntimeError("database is gone")

    registry.add_collector(collect_healthy, [healthy])
    remove_broken = registry.add_collector(collect_broken, [broken])
    broken.set(9, "stale")

    families = parse(asyncio.run(registry.scrape()))

    assert families["demo_healthy"].value(state="up") == 1
    assert families["demo_broken"].samples == []
    assert any("collector" in r.getMessage() for r in caplog.records)

    remove_broken()
    remove_broken()
    broken.set(3, "fresh")
    families = parse(asyncio.run(registry.scrape()))
    assert families["demo_healthy"].value(state="up") == 2
    assert families["demo_broken"].value(state="fresh") == 3


@pytest.mark.unit
def test_concurrent_updates_from_threads_are_not_lost() -> None:
    counter = Counter("demo_total", "help", ("worker",))
    histogram = Histogram("demo_seconds", "help")

    def work() -> None:
        for _ in range(2000):
            counter.inc("w")
            histogram.observe(0.001)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counter.value("w") == 16000
    assert histogram.count() == 16000
