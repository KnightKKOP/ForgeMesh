"""``forgemesh.controlplane.diagnostics`` 单元测试。

覆盖诊断包核心语义：

* DiagnosticBundle：创建与字段校验。
* DiagnosticCollector.collect：各维度聚合。
* 按租户权限受限：只返回该租户数据，不泄露其他租户。
* 日志脱敏：password/token/secret/api_key/credentials → ***。
* 递归脱敏：嵌套 dict 中的敏感字段。
* collect_plugins 全局（不按租户隔离）。
* correlation_id 累计贯穿。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.controlplane.diagnostics import (
    SENSITIVE_FIELDS,
    DiagnosticBundle,
    DiagnosticCollector,
    _redact_sensitive,
)
from forgemesh.controlplane.observability import CorrelationContext, new_correlation_id
from pydantic import ValidationError

# ---------------------------------------------------------------------- #
# DiagnosticBundle
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_bundle_creation_with_all_fields() -> None:
    """DiagnosticBundle 创建并携带所有字段。"""
    bundle = DiagnosticBundle(
        bundle_id="b1",
        tenant_id="t1",
        generated_at=1000.0,
        resources=[{"id": "r1"}],
        workflows=[{"id": "w1"}],
        plugins=[{"id": "p1"}],
        decisions=[{"id": "d1"}],
        logs=[{"msg": "hello"}],
        metrics={"count": 1},
        correlation_id="c1",
    )
    assert bundle.bundle_id == "b1"
    assert bundle.tenant_id == "t1"
    assert bundle.generated_at == 1000.0
    assert bundle.resources == [{"id": "r1"}]
    assert bundle.workflows == [{"id": "w1"}]
    assert bundle.plugins == [{"id": "p1"}]
    assert bundle.decisions == [{"id": "d1"}]
    assert bundle.logs == [{"msg": "hello"}]
    assert bundle.metrics == {"count": 1}
    assert bundle.correlation_id == "c1"


@pytest.mark.unit
def test_bundle_defaults() -> None:
    """DiagnosticBundle 列表/字典字段默认空。"""
    bundle = DiagnosticBundle(
        bundle_id="b1",
        tenant_id="t1",
        generated_at=1000.0,
        correlation_id="c1",
    )
    assert bundle.resources == []
    assert bundle.workflows == []
    assert bundle.plugins == []
    assert bundle.decisions == []
    assert bundle.logs == []
    assert bundle.metrics == {}


@pytest.mark.unit
def test_bundle_extra_forbidden() -> None:
    """DiagnosticBundle 拒绝额外字段。"""
    with pytest.raises(ValidationError):
        DiagnosticBundle(
            bundle_id="b1",
            tenant_id="t1",
            generated_at=1000.0,
            correlation_id="c1",
            unknown_field="x",  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_bundle_required_fields() -> None:
    """缺少必填字段时校验失败。"""
    with pytest.raises(ValidationError):
        DiagnosticBundle(  # type: ignore[call-arg]
            tenant_id="t1",
            generated_at=1000.0,
            correlation_id="c1",
        )


# ---------------------------------------------------------------------- #
# _redact_sensitive
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_redact_sensitive_basic_fields() -> None:
    """敏感字段 password/token/secret/api_key/credentials 替换为 ***。"""
    data = {
        "password": "p@ss",
        "token": "tok123",
        "secret": "s3cr3t",
        "api_key": "ak_xxx",
        "credentials": "cred",
        "normal": "keep",
    }
    redacted = _redact_sensitive(data)
    assert redacted["password"] == "***"
    assert redacted["token"] == "***"
    assert redacted["secret"] == "***"
    assert redacted["api_key"] == "***"
    assert redacted["credentials"] == "***"
    assert redacted["normal"] == "keep"


@pytest.mark.unit
def test_redact_sensitive_case_insensitive() -> None:
    """敏感字段匹配大小写不敏感。"""
    data = {"Password": "x", "TOKEN": "y", "Normal": "z"}
    redacted = _redact_sensitive(data)
    assert redacted["Password"] == "***"
    assert redacted["TOKEN"] == "***"
    assert redacted["Normal"] == "z"


@pytest.mark.unit
def test_redact_sensitive_nested() -> None:
    """递归脱敏嵌套 dict 中的敏感字段。"""
    data = {
        "user": {
            "name": "alice",
            "password": "secret123",
            "meta": {"token": "tok", "age": 30},
        },
        "level": "info",
    }
    redacted = _redact_sensitive(data)
    assert redacted["user"]["name"] == "alice"
    assert redacted["user"]["password"] == "***"
    assert redacted["user"]["meta"]["token"] == "***"
    assert redacted["user"]["meta"]["age"] == 30
    assert redacted["level"] == "info"


@pytest.mark.unit
def test_redact_sensitive_deeply_nested() -> None:
    """多层嵌套 dict 中的敏感字段也被脱敏。"""
    data = {"a": {"b": {"c": {"secret": "deep"}}}}
    redacted = _redact_sensitive(data)
    assert redacted["a"]["b"]["c"]["secret"] == "***"


@pytest.mark.unit
def test_redact_sensitive_does_not_mutate_original() -> None:
    """脱敏不修改原字典。"""
    data = {"password": "secret", "nested": {"token": "tok"}, "normal": "keep"}
    _redact_sensitive(data)
    assert data["password"] == "secret"
    assert data["nested"]["token"] == "tok"
    assert data["normal"] == "keep"


@pytest.mark.unit
def test_redact_sensitive_empty_dict() -> None:
    """空字典脱敏后仍为空字典。"""
    assert _redact_sensitive({}) == {}


@pytest.mark.unit
def test_sensitive_fields_contents() -> None:
    """SENSITIVE_FIELDS 包含预期的字段名。"""
    assert "password" in SENSITIVE_FIELDS
    assert "token" in SENSITIVE_FIELDS
    assert "secret" in SENSITIVE_FIELDS
    assert "api_key" in SENSITIVE_FIELDS
    assert "credentials" in SENSITIVE_FIELDS


# ---------------------------------------------------------------------- #
# DiagnosticCollector — collect 聚合
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_collect_aggregates_all_dimensions() -> None:
    """collect 聚合资源/工作流/插件/决策/日志/指标各维度。"""
    collector = DiagnosticCollector()
    collector.register_resource("t1", {"id": "r1", "type": "gpu"})
    collector.register_workflow("t1", {"id": "w1", "status": "running"})
    collector.register_plugin({"id": "p1", "state": "Enabled"})
    collector.register_decision("t1", {"id": "d1", "action": "allow"})
    collector.register_log("t1", {"message": "hello", "level": "info"})
    collector.set_metrics({"request_count": 10})

    bundle = asyncio.run(collector.collect("t1"))

    assert bundle.tenant_id == "t1"
    assert bundle.resources == [{"id": "r1", "type": "gpu"}]
    assert bundle.workflows == [{"id": "w1", "status": "running"}]
    assert bundle.plugins == [{"id": "p1", "state": "Enabled"}]
    assert bundle.decisions == [{"id": "d1", "action": "allow"}]
    assert bundle.logs == [{"message": "hello", "level": "info"}]
    assert bundle.metrics == {"request_count": 10}


@pytest.mark.unit
def test_collect_generates_bundle_id_and_timestamp() -> None:
    """collect 生成唯一 bundle_id 和 generated_at 时间戳。"""
    collector = DiagnosticCollector()
    bundle1 = asyncio.run(collector.collect("t1"))
    bundle2 = asyncio.run(collector.collect("t1"))
    assert bundle1.bundle_id != bundle2.bundle_id
    assert isinstance(bundle1.generated_at, float)
    assert bundle1.generated_at > 0


# ---------------------------------------------------------------------- #
# 按租户权限受限
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_collect_tenant_isolation() -> None:
    """collect 只返回指定租户的数据，不泄露其他租户。"""
    collector = DiagnosticCollector()
    collector.register_resource("t1", {"id": "r1"})
    collector.register_resource("t2", {"id": "r2"})
    collector.register_workflow("t1", {"id": "w1"})
    collector.register_workflow("t2", {"id": "w2"})
    collector.register_decision("t1", {"id": "d1"})
    collector.register_decision("t2", {"id": "d2"})
    collector.register_log("t1", {"message": "t1-log"})
    collector.register_log("t2", {"message": "t2-log"})

    bundle = asyncio.run(collector.collect("t1"))

    # t1 只看到自己的数据
    assert bundle.resources == [{"id": "r1"}]
    assert bundle.workflows == [{"id": "w1"}]
    assert bundle.decisions == [{"id": "d1"}]
    assert bundle.logs == [{"message": "t1-log"}]
    # 绝不包含 t2 的数据
    assert all(r.get("id") != "r2" for r in bundle.resources)
    assert all(w.get("id") != "w2" for w in bundle.workflows)


@pytest.mark.unit
def test_collect_empty_tenant_returns_empty_lists() -> None:
    """无数据的租户返回空列表（不泄露其他租户存在性）。"""
    collector = DiagnosticCollector()
    collector.register_resource("t1", {"id": "r1"})

    bundle = asyncio.run(collector.collect("t_nonexistent"))
    assert bundle.resources == []
    assert bundle.workflows == []
    assert bundle.decisions == []
    assert bundle.logs == []


@pytest.mark.unit
def test_collect_resources_tenant_isolation() -> None:
    """collect_resources 只返回指定租户的资源。"""
    collector = DiagnosticCollector()
    collector.register_resource("t1", {"id": "r1"})
    collector.register_resource("t2", {"id": "r2"})

    r1 = asyncio.run(collector.collect_resources("t1"))
    r2 = asyncio.run(collector.collect_resources("t2"))
    assert r1 == [{"id": "r1"}]
    assert r2 == [{"id": "r2"}]


@pytest.mark.unit
def test_collect_workflows_tenant_isolation() -> None:
    """collect_workflows 只返回指定租户的工作流。"""
    collector = DiagnosticCollector()
    collector.register_workflow("t1", {"id": "w1"})
    collector.register_workflow("t2", {"id": "w2"})

    w1 = asyncio.run(collector.collect_workflows("t1"))
    assert w1 == [{"id": "w1"}]


@pytest.mark.unit
def test_collect_decisions_tenant_isolation() -> None:
    """collect_decisions 只返回指定租户的决策。"""
    collector = DiagnosticCollector()
    collector.register_decision("t1", {"id": "d1"})
    collector.register_decision("t2", {"id": "d2"})

    d1 = asyncio.run(collector.collect_decisions("t1"))
    assert d1 == [{"id": "d1"}]


# ---------------------------------------------------------------------- #
# collect_plugins 全局
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_collect_plugins_global() -> None:
    """collect_plugins 全局返回所有插件，不按租户隔离。"""
    collector = DiagnosticCollector()
    collector.register_plugin({"id": "p1"})
    collector.register_plugin({"id": "p2"})

    plugins = asyncio.run(collector.collect_plugins())
    assert plugins == [{"id": "p1"}, {"id": "p2"}]


@pytest.mark.unit
def test_collect_plugins_global_in_bundle() -> None:
    """bundle 中插件为全局数据，与租户无关。"""
    collector = DiagnosticCollector()
    collector.register_plugin({"id": "p1"})

    bundle = asyncio.run(collector.collect("any_tenant"))
    assert bundle.plugins == [{"id": "p1"}]

    bundle2 = asyncio.run(collector.collect("other_tenant"))
    assert bundle2.plugins == [{"id": "p1"}]


# ---------------------------------------------------------------------- #
# 日志脱敏
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_collect_logs_redaction() -> None:
    """collect_logs 对敏感字段脱敏。"""
    collector = DiagnosticCollector()
    collector.register_log(
        "t1",
        {
            "message": "login",
            "password": "secret123",
            "token": "tok",
            "secret": "s",
            "api_key": "ak",
            "credentials": "cr",
        },
    )

    logs = asyncio.run(collector.collect_logs("t1", None))
    assert logs[0]["password"] == "***"
    assert logs[0]["token"] == "***"
    assert logs[0]["secret"] == "***"
    assert logs[0]["api_key"] == "***"
    assert logs[0]["credentials"] == "***"
    assert logs[0]["message"] == "login"


@pytest.mark.unit
def test_collect_logs_nested_redaction() -> None:
    """collect_logs 递归脱敏嵌套字典中的敏感字段。"""
    collector = DiagnosticCollector()
    collector.register_log(
        "t1",
        {
            "message": "request",
            "context": {
                "user": "alice",
                "password": "pw",
                "headers": {"authorization": "Bearer x", "token": "t"},
            },
        },
    )

    logs = asyncio.run(collector.collect_logs("t1", None))
    assert logs[0]["context"]["user"] == "alice"
    assert logs[0]["context"]["password"] == "***"
    assert logs[0]["context"]["headers"]["authorization"] == "Bearer x"
    assert logs[0]["context"]["headers"]["token"] == "***"


@pytest.mark.unit
def test_collect_redaction_in_bundle() -> None:
    """collect 生成的 bundle 中日志已脱敏。"""
    collector = DiagnosticCollector()
    collector.register_log("t1", {"message": "m", "password": "pw"})

    bundle = asyncio.run(collector.collect("t1"))
    assert bundle.logs[0]["password"] == "***"
    assert bundle.logs[0]["message"] == "m"


@pytest.mark.unit
def test_collect_logs_does_not_mutate_internal() -> None:
    """collect_logs 脱敏不影响内部存储的原始数据。"""
    collector = DiagnosticCollector()
    collector.register_log("t1", {"message": "m", "password": "pw"})

    asyncio.run(collector.collect_logs("t1", None))
    # 再次采集，原始密码仍在（脱敏只作用于输出）
    logs = asyncio.run(collector.collect_logs("t1", None))
    assert logs[0]["password"] == "***"


# ---------------------------------------------------------------------- #
# collect_logs 时间范围过滤
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_collect_logs_time_range_filter() -> None:
    """collect_logs 按 time_range 过滤日志。"""
    collector = DiagnosticCollector()
    collector.register_log("t1", {"message": "old", "timestamp": 100.0})
    collector.register_log("t1", {"message": "mid", "timestamp": 200.0})
    collector.register_log("t1", {"message": "new", "timestamp": 300.0})

    logs = asyncio.run(collector.collect_logs("t1", (150.0, 250.0)))
    assert len(logs) == 1
    assert logs[0]["message"] == "mid"


@pytest.mark.unit
def test_collect_logs_time_range_boundaries() -> None:
    """time_range 边界值包含在内（闭区间）。"""
    collector = DiagnosticCollector()
    collector.register_log("t1", {"message": "start", "timestamp": 100.0})
    collector.register_log("t1", {"message": "end", "timestamp": 200.0})

    logs = asyncio.run(collector.collect_logs("t1", (100.0, 200.0)))
    assert len(logs) == 2


@pytest.mark.unit
def test_collect_logs_no_timestamp_excluded_with_range() -> None:
    """无 timestamp 字段的日志在指定 time_range 时被排除。"""
    collector = DiagnosticCollector()
    collector.register_log("t1", {"message": "no-ts"})
    collector.register_log("t1", {"message": "with-ts", "timestamp": 200.0})

    logs = asyncio.run(collector.collect_logs("t1", (100.0, 300.0)))
    assert len(logs) == 1
    assert logs[0]["message"] == "with-ts"


@pytest.mark.unit
def test_collect_logs_no_range_returns_all() -> None:
    """无 time_range 时返回全部日志（含无 timestamp 的）。"""
    collector = DiagnosticCollector()
    collector.register_log("t1", {"message": "no-ts"})
    collector.register_log("t1", {"message": "with-ts", "timestamp": 200.0})

    all_logs = asyncio.run(collector.collect_logs("t1", None))
    assert len(all_logs) == 2


@pytest.mark.unit
def test_collect_logs_tenant_isolation() -> None:
    """collect_logs 只返回指定租户的日志。"""
    collector = DiagnosticCollector()
    collector.register_log("t1", {"message": "t1-log", "password": "pw1"})
    collector.register_log("t2", {"message": "t2-log", "password": "pw2"})

    logs = asyncio.run(collector.collect_logs("t1", None))
    assert len(logs) == 1
    assert logs[0]["message"] == "t1-log"
    assert logs[0]["password"] == "***"


# ---------------------------------------------------------------------- #
# correlation_id 累计贯穿
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_collect_correlation_id_generated() -> None:
    """无上下文时 collect 生成新的 correlation_id。"""
    collector = DiagnosticCollector()
    bundle = asyncio.run(collector.collect("t1"))
    assert bundle.correlation_id is not None
    assert len(bundle.correlation_id) > 0


@pytest.mark.unit
def test_collect_correlation_id_reused_from_context() -> None:
    """collect 复用 CorrelationContext 中的 correlation_id。"""
    cid = new_correlation_id()
    collector = DiagnosticCollector()
    with CorrelationContext(correlation_id=cid):
        bundle = asyncio.run(collector.collect("t1"))
    assert bundle.correlation_id == cid


@pytest.mark.unit
def test_collect_correlation_id_unique_per_call() -> None:
    """无上下文时每次 collect 生成不同的 correlation_id。"""
    collector = DiagnosticCollector()
    b1 = asyncio.run(collector.collect("t1"))
    b2 = asyncio.run(collector.collect("t1"))
    assert b1.correlation_id != b2.correlation_id


@pytest.mark.unit
def test_collect_correlation_id_context_reset() -> None:
    """退出 CorrelationContext 后 collect 重新生成 correlation_id。"""
    cid = new_correlation_id()
    collector = DiagnosticCollector()
    with CorrelationContext(correlation_id=cid):
        bundle_in = asyncio.run(collector.collect("t1"))
    bundle_out = asyncio.run(collector.collect("t1"))
    assert bundle_in.correlation_id == cid
    assert bundle_out.correlation_id != cid


# ---------------------------------------------------------------------- #
# collect_metrics
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_collect_metrics_snapshot() -> None:
    """collect_metrics 返回指标快照。"""
    collector = DiagnosticCollector()
    collector.set_metrics({"a": 1, "b": 2})
    assert collector.collect_metrics() == {"a": 1, "b": 2}


@pytest.mark.unit
def test_collect_metrics_empty_default() -> None:
    """未设置指标时 collect_metrics 返回空字典。"""
    collector = DiagnosticCollector()
    assert collector.collect_metrics() == {}


@pytest.mark.unit
def test_collect_metrics_does_not_mutate_internal() -> None:
    """collect_metrics 返回副本，不暴露内部状态。"""
    collector = DiagnosticCollector()
    collector.set_metrics({"a": 1})
    snapshot = collector.collect_metrics()
    snapshot["a"] = 999
    assert collector.collect_metrics() == {"a": 1}


@pytest.mark.unit
def test_collect_metrics_in_bundle() -> None:
    """collect 生成的 bundle 携带指标快照。"""
    collector = DiagnosticCollector()
    collector.set_metrics({"request_count": 5})

    bundle = asyncio.run(collector.collect("t1"))
    assert bundle.metrics == {"request_count": 5}
