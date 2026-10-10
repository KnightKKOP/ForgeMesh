"""qos-basic 插件单元测试（对齐 development-plan WP-4.3）。

覆盖：

* ``QoSProvider.evaluate`` 准入（正常请求）。
* ``QoSProvider.evaluate`` 拒绝——限流（``rate_limited``）。
* ``QoSProvider.evaluate`` 拒绝——并发满（``max_concurrent_reached``）。
* ``QoSProviderResult`` 字段正确性。
* 优先级解析（字符串 / int / 默认 / 无法识别回退）。
* 与 :class:`QoSManager` 集成（``release_slot`` 后恢复准入）。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.policy.qos import Priority, QoSConfig
from qos_basic_plugin import QoSProvider, QoSProviderResult


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """为每个测试提供干净的 current event loop（兼容 3.9–3.12）。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(coro):
    """在当前 event loop 上运行协程并返回结果。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def _request(**overrides: object) -> dict[str, object]:
    """构造请求字典，提供合理默认值。"""
    req: dict[str, object] = {
        "request_id": "req-1",
        "tenant_id": "tenant-a",
        "priority": "NORMAL",
    }
    req.update(overrides)
    return req


# --------------------------------------------------------------------------- #
# QoSProviderResult
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_provider_result_creation() -> None:
    """QoSProviderResult 应正确创建并保存字段。"""
    result = QoSProviderResult(
        allowed=True,
        reason="admitted",
        priority="NORMAL",
        queue_position=0,
    )
    assert result.allowed is True
    assert result.reason == "admitted"
    assert result.priority == "NORMAL"
    assert result.queue_position == 0


@pytest.mark.unit
def test_provider_result_rejected_fields() -> None:
    """拒绝结果应正确反映 allowed=False 与 queue_position=None。"""
    result = QoSProviderResult(
        allowed=False,
        reason="rate_limited",
        priority="HIGH",
        queue_position=None,
    )
    assert result.allowed is False
    assert result.reason == "rate_limited"
    assert result.priority == "HIGH"
    assert result.queue_position is None


@pytest.mark.unit
def test_provider_result_rejects_extra_fields() -> None:
    """extra="forbid" 应拒绝多余字段。"""
    with pytest.raises(ValueError):  # noqa: PT011 — pydantic ValidationError 子类
        QoSProviderResult(
            allowed=True,
            reason="admitted",
            priority="NORMAL",
            queue_position=0,
            unknown_field="bad",
        )


# --------------------------------------------------------------------------- #
# QoSProvider.evaluate — 准入
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_evaluate_admitted() -> None:
    """正常请求应被准入，返回 admitted 结果。"""
    config = QoSConfig(max_concurrent=10)
    provider = QoSProvider(config)

    result = _run(provider.evaluate(_request()))

    assert isinstance(result, QoSProviderResult)
    assert result.allowed is True
    assert result.reason == "admitted"
    assert result.priority == "NORMAL"
    assert result.queue_position == 0


@pytest.mark.unit
def test_evaluate_admitted_with_high_priority() -> None:
    """高优先级租户应被准入，priority 反映配置的优先级。"""
    config = QoSConfig(
        tenant_priorities={"tenant-a": Priority.HIGH},
        max_concurrent=10,
    )
    provider = QoSProvider(config)

    result = _run(provider.evaluate(_request(priority="HIGH")))

    assert result.allowed is True
    assert result.priority == "HIGH"


# --------------------------------------------------------------------------- #
# QoSProvider.evaluate — 拒绝：限流
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_evaluate_rate_limited() -> None:
    """令牌耗尽后应被限流拒绝（reason=rate_limited）。"""
    config = QoSConfig(
        max_concurrent=10,
        rate_limit_per_tenant={"tenant-a": 1},
    )
    provider = QoSProvider(config)

    first = _run(provider.evaluate(_request(request_id="r1")))
    assert first.allowed is True

    # 令牌桶 burst=1 已耗尽，立即第二次请求应被限流。
    second = _run(provider.evaluate(_request(request_id="r2")))
    assert second.allowed is False
    assert second.reason == "rate_limited"
    assert second.queue_position is None


# --------------------------------------------------------------------------- #
# QoSProvider.evaluate — 拒绝：并发满
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_evaluate_max_concurrent_reached() -> None:
    """全局并发数达上限后应拒绝（reason=max_concurrent_reached）。"""
    config = QoSConfig(max_concurrent=1)
    provider = QoSProvider(config)

    first = _run(provider.evaluate(_request(request_id="r1")))
    assert first.allowed is True

    # 并发槽已占满，第二次请求应被拒绝。
    second = _run(provider.evaluate(_request(request_id="r2")))
    assert second.allowed is False
    assert second.reason == "max_concurrent_reached"
    assert second.queue_position is None


@pytest.mark.unit
def test_evaluate_max_concurrent_zero_rejects_all() -> None:
    """max_concurrent=0 时所有请求应被拒绝。"""
    config = QoSConfig(max_concurrent=0)
    provider = QoSProvider(config)

    result = _run(provider.evaluate(_request()))
    assert result.allowed is False
    assert result.reason == "max_concurrent_reached"


# --------------------------------------------------------------------------- #
# 优先级解析
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_evaluate_priority_from_string() -> None:
    """priority 以字符串形式传入应正确解析。"""
    config = QoSConfig(max_concurrent=10)
    provider = QoSProvider(config)

    result = _run(provider.evaluate(_request(priority="critical")))
    assert result.priority == "CRITICAL"


@pytest.mark.unit
def test_evaluate_priority_from_int() -> None:
    """priority 以 int 形式传入应正确解析为枚举值。"""
    config = QoSConfig(max_concurrent=10)
    provider = QoSProvider(config)

    result = _run(provider.evaluate(_request(priority=1)))
    assert result.priority == "HIGH"


@pytest.mark.unit
def test_evaluate_priority_default_when_missing() -> None:
    """缺少 priority 字段时应默认 NORMAL。"""
    config = QoSConfig(max_concurrent=10)
    provider = QoSProvider(config)


    req: dict[str, object] = {"request_id": "r1", "tenant_id": "t1"}
    result = _run(provider.evaluate(req))
    assert result.priority == "NORMAL"


@pytest.mark.unit
def test_evaluate_priority_unrecognized_falls_back_to_normal() -> None:
    """无法识别的 priority 字符串应回退到 NORMAL。"""
    config = QoSConfig(max_concurrent=10)
    provider = QoSProvider(config)

    result = _run(provider.evaluate(_request(priority="unknown")))
    assert result.priority == "NORMAL"


# --------------------------------------------------------------------------- #
# 请求解析
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_evaluate_missing_tenant_id_raises_key_error() -> None:
    """缺少 tenant_id 应抛 KeyError。"""
    config = QoSConfig(max_concurrent=10)
    provider = QoSProvider(config)

    with pytest.raises(KeyError):
        _run(provider.evaluate({"request_id": "r1"}))


@pytest.mark.unit
def test_evaluate_auto_generates_request_id() -> None:
    """缺少 request_id 时应自动生成，不影响准入。"""
    config = QoSConfig(max_concurrent=10)
    provider = QoSProvider(config)

    result = _run(provider.evaluate({"tenant_id": "tenant-a"}))
    assert result.allowed is True


# --------------------------------------------------------------------------- #
# 与 QoSManager 集成
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_integration_release_slot_restores_admission() -> None:
    """释放并发槽后应恢复准入（与 QoSManager 集成）。"""
    config = QoSConfig(max_concurrent=1)
    provider = QoSProvider(config)

    first = _run(provider.evaluate(_request(request_id="r1")))
    assert first.allowed is True

    # 并发满
    second = _run(provider.evaluate(_request(request_id="r2")))
    assert second.allowed is False

    # 释放槽后可再次准入
    _run(provider.manager.release_slot("tenant-a"))
    third = _run(provider.evaluate(_request(request_id="r3")))
    assert third.allowed is True
    assert third.reason == "admitted"


@pytest.mark.unit
def test_integration_manager_property_exposes_qos_manager() -> None:
    """manager 属性应暴露内部 QoSManager 实例。"""
    from forgemesh.policy.qos import QoSManager

    config = QoSConfig(max_concurrent=5)
    provider = QoSProvider(config)

    assert isinstance(provider.manager, QoSManager)


@pytest.mark.unit
def test_integration_multi_tenant_concurrent_independent() -> None:
    """不同租户共享全局并发上限，但各自占用独立计数。"""
    config = QoSConfig(max_concurrent=2)
    provider = QoSProvider(config)

    a1 = _run(provider.evaluate(_request(request_id="a1", tenant_id="tenant-a")))
    b1 = _run(provider.evaluate(_request(request_id="b1", tenant_id="tenant-b")))
    assert a1.allowed is True
    assert b1.allowed is True

    # 全局并发已满（2/2），第三个请求无论租户均被拒绝
    c1 = _run(provider.evaluate(_request(request_id="c1", tenant_id="tenant-c")))
    assert c1.allowed is False
    assert c1.reason == "max_concurrent_reached"
