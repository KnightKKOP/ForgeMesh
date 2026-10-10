"""``forgemesh.controlplane.observability`` 单元测试。

覆盖可观测性三件套的核心语义：

* CorrelationContext：唯一 ID 生成、contextvars 异步传播。
* MetricsRecorder：counter / gauge / histogram 记录与读取、8 类关键指标。
* StructuredLogger：JSON 格式、敏感字段脱敏、各级别便捷方法。
* TraceSpan：start / end / duration、add_attribute、parent 链。
* TraceRecorder：多 span、correlation_id 累计贯穿。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from forgemesh.controlplane.observability import (
    KEY_METRICS,
    SENSITIVE_FIELDS,
    CorrelationContext,
    MetricsRecorder,
    StructuredLogger,
    TraceRecorder,
    TraceSpan,
    new_correlation_id,
)

# ---------------------------------------------------------------------- #
# CorrelationContext
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_new_correlation_id_unique() -> None:
    """new_correlation_id 生成唯一 ID。"""
    ids = {new_correlation_id() for _ in range(1000)}
    assert len(ids) == 1000


@pytest.mark.unit
def test_new_correlation_id_is_string() -> None:
    """correlation_id 是字符串。"""
    cid = new_correlation_id()
    assert isinstance(cid, str)
    assert len(cid) > 0


@pytest.mark.unit
def test_correlation_context_set_and_get() -> None:
    """set 后可 get，reset 后恢复 None。"""
    cid = new_correlation_id()
    tokens = CorrelationContext.set(correlation_id=cid, tenant_id="t1", request_id="r1", operation="op1")
    assert CorrelationContext.get_correlation_id() == cid
    assert CorrelationContext.get_tenant_id() == "t1"
    assert CorrelationContext.get_request_id() == "r1"
    assert CorrelationContext.get_operation() == "op1"
    CorrelationContext.reset(tokens)
    assert CorrelationContext.get_correlation_id() is None
    assert CorrelationContext.get_tenant_id() is None


@pytest.mark.unit
def test_correlation_context_current() -> None:
    """current() 返回上下文快照。"""
    cid = new_correlation_id()
    tokens = CorrelationContext.set(correlation_id=cid, tenant_id="t2")
    ctx = CorrelationContext.current()
    assert ctx.correlation_id == cid
    assert ctx.tenant_id == "t2"
    assert ctx.request_id is None
    CorrelationContext.reset(tokens)


@pytest.mark.unit
def test_correlation_context_context_manager() -> None:
    """bind 上下文管理器进出自动设置/恢复。"""
    cid = new_correlation_id()
    assert CorrelationContext.get_correlation_id() is None
    with CorrelationContext(correlation_id=cid, tenant_id="t3"):
        assert CorrelationContext.get_correlation_id() == cid
        assert CorrelationContext.get_tenant_id() == "t3"
    assert CorrelationContext.get_correlation_id() is None


@pytest.mark.unit
def test_correlation_context_async_propagation() -> None:
    """contextvars 在 asyncio 任务间自动传播。"""

    async def child() -> str | None:
        return CorrelationContext.get_correlation_id()

    async def main() -> str | None:
        cid = new_correlation_id()
        tokens = CorrelationContext.set(correlation_id=cid)
        try:
            # 创建子任务，contextvars 自动复制
            result = await asyncio.create_task(child())
            return result
        finally:
            CorrelationContext.reset(tokens)

    result = asyncio.run(main())
    assert result is not None
    assert isinstance(result, str)


# ---------------------------------------------------------------------- #
# MetricsRecorder
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_counter_accumulate() -> None:
    """counter 相同 name+tags 累加。"""
    recorder = MetricsRecorder()
    recorder.record_counter("request_count", 1, {"tenant": "a"})
    recorder.record_counter("request_count", 2, {"tenant": "a"})
    recorder.record_counter("request_count", 1, {"tenant": "b"})

    metrics = recorder.get_metrics()
    by_tags = {tuple(sorted(c["tags"].items())): c["value"] for c in metrics["counters"]}
    assert by_tags[(("tenant", "a"),)] == 3.0
    assert by_tags[(("tenant", "b"),)] == 1.0


@pytest.mark.unit
def test_counter_default_value() -> None:
    """counter 默认 value=1。"""
    recorder = MetricsRecorder()
    recorder.record_counter("error_count")
    metrics = recorder.get_metrics()
    assert metrics["counters"][0]["value"] == 1.0


@pytest.mark.unit
def test_counter_no_tags() -> None:
    """counter 无 tags 也能记录。"""
    recorder = MetricsRecorder()
    recorder.record_counter("decision_count", 5)
    metrics = recorder.get_metrics()
    assert len(metrics["counters"]) == 1
    assert metrics["counters"][0]["value"] == 5.0
    assert metrics["counters"][0]["tags"] == {}


@pytest.mark.unit
def test_gauge_overwrite() -> None:
    """gauge 相同 name+tags 覆盖。"""
    recorder = MetricsRecorder()
    recorder.record_gauge("active_workflows", 3.0)
    recorder.record_gauge("active_workflows", 7.0)
    metrics = recorder.get_metrics()
    assert len(metrics["gauges"]) == 1
    assert metrics["gauges"][0]["value"] == 7.0


@pytest.mark.unit
def test_gauge_with_tags() -> None:
    """gauge 不同 tags 分别记录。"""
    recorder = MetricsRecorder()
    recorder.record_gauge("quota_usage", 80.0, {"tenant": "a"})
    recorder.record_gauge("quota_usage", 40.0, {"tenant": "b"})
    metrics = recorder.get_metrics()
    assert len(metrics["gauges"]) == 2
    values = sorted(g["value"] for g in metrics["gauges"])
    assert values == [40.0, 80.0]


@pytest.mark.unit
def test_histogram_collect_values() -> None:
    """histogram 收集所有采样值。"""
    recorder = MetricsRecorder()
    recorder.record_histogram("request_latency_ms", 10.0)
    recorder.record_histogram("request_latency_ms", 20.0)
    recorder.record_histogram("request_latency_ms", 30.0)
    metrics = recorder.get_metrics()
    assert len(metrics["histograms"]) == 1
    assert metrics["histograms"][0]["values"] == [10.0, 20.0, 30.0]


@pytest.mark.unit
def test_histogram_with_tags() -> None:
    """histogram 不同 tags 分别收集。"""
    recorder = MetricsRecorder()
    recorder.record_histogram("request_latency_ms", 10.0, {"route": "/a"})
    recorder.record_histogram("request_latency_ms", 50.0, {"route": "/b"})
    metrics = recorder.get_metrics()
    assert len(metrics["histograms"]) == 2


@pytest.mark.unit
def test_key_metrics_eight_categories() -> None:
    """8 类关键指标名称齐全。"""
    expected = {
        "request_count",
        "request_latency_ms",
        "active_workflows",
        "decision_count",
        "plugin_invocation_count",
        "allocation_count",
        "quota_usage",
        "error_count",
    }
    assert expected == KEY_METRICS
    assert len(KEY_METRICS) == 8


@pytest.mark.unit
def test_all_key_metrics_recordable() -> None:
    """8 类关键指标均可通过 recorder 记录。"""
    recorder = MetricsRecorder()
    recorder.record_counter("request_count", 10)
    recorder.record_histogram("request_latency_ms", 42.0)
    recorder.record_gauge("active_workflows", 3.0)
    recorder.record_counter("decision_count", 5)
    recorder.record_counter("plugin_invocation_count", 20)
    recorder.record_counter("allocation_count", 2)
    recorder.record_gauge("quota_usage", 75.0)
    recorder.record_counter("error_count", 1)

    metrics = recorder.get_metrics()
    all_names = (
        {c["name"] for c in metrics["counters"]}
        | {g["name"] for g in metrics["gauges"]}
        | {h["name"] for h in metrics["histograms"]}
    )
    assert all_names >= KEY_METRICS


@pytest.mark.unit
def test_get_metrics_structure() -> None:
    """get_metrics 返回包含 counters/gauges/histograms 的字典。"""
    recorder = MetricsRecorder()
    recorder.record_counter("request_count", 1)
    recorder.record_gauge("active_workflows", 1.0)
    recorder.record_histogram("request_latency_ms", 1.0)
    metrics = recorder.get_metrics()
    assert set(metrics.keys()) == {"counters", "gauges", "histograms"}
    assert isinstance(metrics["counters"], list)
    assert isinstance(metrics["gauges"], list)
    assert isinstance(metrics["histograms"], list)


# ---------------------------------------------------------------------- #
# StructuredLogger
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_sensitive_fields_set() -> None:
    """SENSITIVE_FIELDS 包含预期字段。"""
    assert "password" in SENSITIVE_FIELDS
    assert "token" in SENSITIVE_FIELDS
    assert "secret" in SENSITIVE_FIELDS
    assert "api_key" in SENSITIVE_FIELDS
    assert "credentials" in SENSITIVE_FIELDS


@pytest.mark.unit
def test_logger_redacts_password() -> None:
    """password 字段自动脱敏为 ***。"""
    logger = StructuredLogger()
    logger.info("user login", password="super-secret-123")
    records = logger.get_records()
    assert len(records) == 1
    assert records[0]["password"] == "***"
    assert "super-secret-123" not in json.dumps(records)


@pytest.mark.unit
def test_logger_redacts_token() -> None:
    """token 字段自动脱敏。"""
    logger = StructuredLogger()
    logger.info("auth", token="bearer-xyz")
    assert logger.get_records()[0]["token"] == "***"


@pytest.mark.unit
def test_logger_redacts_secret() -> None:
    """secret 字段自动脱敏。"""
    logger = StructuredLogger()
    logger.info("config", secret="my-secret")
    assert logger.get_records()[0]["secret"] == "***"


@pytest.mark.unit
def test_logger_redacts_api_key() -> None:
    """api_key 字段自动脱敏。"""
    logger = StructuredLogger()
    logger.info("api call", api_key="key-123")
    assert logger.get_records()[0]["api_key"] == "***"


@pytest.mark.unit
def test_logger_redacts_credentials() -> None:
    """credentials 字段自动脱敏。"""
    logger = StructuredLogger()
    logger.info("connect", credentials={"user": "admin", "pass": "pw"})
    assert logger.get_records()[0]["credentials"] == "***"


@pytest.mark.unit
def test_logger_preserves_normal_fields() -> None:
    """非敏感字段保持原值。"""
    logger = StructuredLogger()
    logger.info("request", method="GET", path="/api/v1/services", status=200)
    rec = logger.get_records()[0]
    assert rec["method"] == "GET"
    assert rec["path"] == "/api/v1/services"
    assert rec["status"] == 200


@pytest.mark.unit
def test_logger_redacts_case_insensitive() -> None:
    """敏感字段匹配不区分大小写。"""
    logger = StructuredLogger()
    logger.info("test", Password="pw", TOKEN="tk", API_KEY="ak")
    rec = logger.get_records()[0]
    assert rec["Password"] == "***"
    assert rec["TOKEN"] == "***"
    assert rec["API_KEY"] == "***"


@pytest.mark.unit
def test_logger_json_format() -> None:
    """日志可通过 sink 以 JSON 格式输出。"""
    captured: list[str] = []
    logger = StructuredLogger(name="test-logger", sink=captured.append)
    logger.info("hello world", user="alice")
    assert len(captured) == 1
    parsed = json.loads(captured[0])
    assert parsed["level"] == "info"
    assert parsed["message"] == "hello world"
    assert parsed["logger"] == "test-logger"
    assert parsed["user"] == "alice"
    assert "timestamp" in parsed


@pytest.mark.unit
def test_logger_levels() -> None:
    """info / warn / error / debug 各级别便捷方法。"""
    logger = StructuredLogger()
    logger.info("info msg")
    logger.warn("warn msg")
    logger.error("error msg")
    logger.debug("debug msg")
    records = logger.get_records()
    assert len(records) == 4
    assert records[0]["level"] == "info"
    assert records[1]["level"] == "warn"
    assert records[2]["level"] == "error"
    assert records[3]["level"] == "debug"


@pytest.mark.unit
def test_logger_correlation_id_explicit() -> None:
    """显式传入 correlation_id 出现在日志中。"""
    logger = StructuredLogger()
    cid = new_correlation_id()
    logger.info("msg", correlation_id=cid)
    assert logger.get_records()[0]["correlation_id"] == cid


@pytest.mark.unit
def test_logger_correlation_id_from_context() -> None:
    """未显式传入时从 contextvars 读取 correlation_id。"""
    logger = StructuredLogger()
    cid = new_correlation_id()
    tokens = CorrelationContext.set(correlation_id=cid)
    try:
        logger.info("msg")
    finally:
        CorrelationContext.reset(tokens)
    assert logger.get_records()[0]["correlation_id"] == cid


# ---------------------------------------------------------------------- #
# TraceSpan
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_span_start_end_duration() -> None:
    """span start/end 后 duration_ms 为非负值。"""
    span = TraceSpan("test-span", "corr-1")
    assert span.duration_ms == 0.0
    span.start()
    span.end()
    assert span.duration_ms >= 0.0
    assert span.ended


@pytest.mark.unit
def test_span_duration_positive_after_sleep() -> None:
    """span 持续时间随实际耗时增长。"""
    span = TraceSpan("test-span", "corr-1")
    span.start()
    # busy wait ~1ms
    end = span._start_time + 0.001  # type: ignore[attr-defined]
    while __import__("time").time() < end:
        pass
    span.end()
    assert span.duration_ms > 0.0


@pytest.mark.unit
def test_span_add_attribute() -> None:
    """add_attribute 添加属性。"""
    span = TraceSpan("test-span", "corr-1")
    span.add_attribute("http.method", "GET")
    span.add_attribute("http.status", 200)
    assert span.attributes["http.method"] == "GET"
    assert span.attributes["http.status"] == 200


@pytest.mark.unit
def test_span_ids_unique() -> None:
    """每个 span 有唯一 span_id。"""
    s1 = TraceSpan("a", "corr-1")
    s2 = TraceSpan("b", "corr-1")
    assert s1.span_id != s2.span_id


@pytest.mark.unit
def test_span_parent_link() -> None:
    """子 span 的 parent_id 指向父 span。"""
    parent = TraceSpan("parent", "corr-1")
    parent.start()
    try:
        child = TraceSpan("child", "corr-1")
        # 模拟从 contextvars 读取 parent
        from forgemesh.controlplane.observability import _parent_span_id_var

        child = TraceSpan("child", "corr-1", parent_id=_parent_span_id_var.get())
        assert child.parent_id == parent.span_id
    finally:
        parent.end()


# ---------------------------------------------------------------------- #
# TraceRecorder
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_recorder_multiple_spans() -> None:
    """TraceRecorder 记录多个 span。"""
    recorder = TraceRecorder()
    s1 = recorder.start_span("span-1", "corr-1")
    s2 = recorder.start_span("span-2", "corr-1")
    recorder.end_span(s2)
    recorder.end_span(s1)
    spans = recorder.get_spans()
    assert len(spans) == 2
    assert spans[0].name == "span-1"
    assert spans[1].name == "span-2"


@pytest.mark.unit
def test_recorder_correlation_id_persists() -> None:
    """correlation_id 累计贯穿所有 span。"""
    recorder = TraceRecorder()
    cid = "req-trace-001"
    s1 = recorder.start_span("create", cid)
    s2 = recorder.start_span("allocate", cid)
    s3 = recorder.start_span("deploy", cid)
    recorder.end_span(s3)
    recorder.end_span(s2)
    recorder.end_span(s1)

    spans = recorder.get_spans()
    assert len(spans) == 3
    for span in spans:
        assert span.correlation_id == cid


@pytest.mark.unit
def test_recorder_parent_chain() -> None:
    """嵌套 span 形成 parent 链：root → child → grandchild。"""
    recorder = TraceRecorder()
    cid = "req-chain-001"

    root = recorder.start_span("root", cid)
    child = recorder.start_span("child", cid)
    grandchild = recorder.start_span("grandchild", cid)
    recorder.end_span(grandchild)
    recorder.end_span(child)
    recorder.end_span(root)

    spans = recorder.get_spans()
    assert len(spans) == 3

    # root 无父
    assert spans[0].parent_id is None
    # child 的父是 root
    assert spans[1].parent_id == spans[0].span_id
    # grandchild 的父是 child
    assert spans[2].parent_id == spans[1].span_id


@pytest.mark.unit
def test_recorder_sibling_spans_share_parent() -> None:
    """同级 span（非嵌套）共享同一 parent。"""
    recorder = TraceRecorder()
    cid = "req-sib-001"

    root = recorder.start_span("root", cid)
    recorder.end_span(root)

    # root 结束后 parent 恢复 None，两个同级 span 都以 None 为 parent
    s1 = recorder.start_span("sibling-1", cid)
    recorder.end_span(s1)
    s2 = recorder.start_span("sibling-2", cid)
    recorder.end_span(s2)

    spans = recorder.get_spans()
    assert len(spans) == 3
    # root、sibling-1、sibling-2 都没有 parent
    assert spans[0].parent_id is None
    assert spans[1].parent_id is None
    assert spans[2].parent_id is None


@pytest.mark.unit
def test_recorder_span_attributes() -> None:
    """span 属性可通过 add_attribute 设置。"""
    recorder = TraceRecorder()
    cid = "req-attr-001"
    span = recorder.start_span("http.request", cid)
    span.add_attribute("http.method", "POST")
    span.add_attribute("http.url", "/api/v1/allocate")
    span.add_attribute("http.status_code", 201)
    recorder.end_span(span)

    spans = recorder.get_spans()
    assert spans[0].attributes["http.method"] == "POST"
    assert spans[0].attributes["http.url"] == "/api/v1/allocate"
    assert spans[0].attributes["http.status_code"] == 201


@pytest.mark.unit
def test_recorder_span_duration_recorded() -> None:
    """所有 span 结束后 duration_ms 均为非负。"""
    recorder = TraceRecorder()
    cid = "req-dur-001"
    for name in ("a", "b", "c"):
        s = recorder.start_span(name, cid)
        recorder.end_span(s)
    for span in recorder.get_spans():
        assert span.duration_ms >= 0.0
        assert span.ended
