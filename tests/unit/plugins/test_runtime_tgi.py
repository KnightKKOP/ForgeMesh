"""runtime-tgi 插件单元测试。

覆盖：

* **deploy** — 部署实例 / TGI 特定参数（sharded / quantize）。
* **verify** — 冒烟验证通过 / 失败 / 异常 / 无 endpoint。
* **release** — 完整释放 / 实例不存在 / 租约失败 / 分配核验失败。
* **health_check** — 健康 / 不健康 / 异常 / 无 endpoint。
* **查询** — get_instance / list_instances。
* **契约一致性** — deploy → verify → release 闭环与 runtime-vllm 行为一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from runtime_tgi_plugin.provider import (
    HealthStatus,
    InstanceStatus,
    ResourceAllocation,
    TgiDeployConfig,
    TgiInstance,
    TgiRuntimeProvider,
    VerifyResult,
)


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """为每个测试提供干净的 current event loop。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(coro: Any) -> Any:
    """在当前事件循环上运行协程并返回结果。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_deploy_config(
    *,
    model_id: str = "meta-llama/Llama-3-8B-Instruct",
    port: int = 8080,
    gpu_ids: list[int] | None = None,
    sharded: bool = False,
    quantize: str | None = None,
) -> TgiDeployConfig:
    """构造测试用 TgiDeployConfig。"""
    return TgiDeployConfig(
        model_id=model_id,
        port=port,
        gpu_ids=gpu_ids if gpu_ids is not None else [0],
        sharded=sharded,
        quantize=quantize,
    )


def _make_allocation(
    *,
    lease_id: str = "lease-1",
    device_ids: list[int] | None = None,
) -> ResourceAllocation:
    """构造测试用 ResourceAllocation。"""
    return ResourceAllocation(
        lease_id=lease_id,
        device_ids=device_ids if device_ids is not None else [0],
    )


def _make_provider(
    *,
    launcher=None,
    http_client=None,
    lease_releaser=None,
    allocation_verifier=None,
) -> TgiRuntimeProvider:
    """构造测试用 TgiRuntimeProvider，默认注入成功 mock。"""

    async def default_launcher(config: TgiDeployConfig, instance_id: str) -> str:
        return f"http://localhost:{config.port}"

    async def default_http(endpoint: str, prompt: str) -> dict[str, Any]:
        return {"ok": True, "latency_ms": 50.0, "text": "hi", "status": 200}

    async def default_releaser(lease_id: str) -> bool:
        return True

    async def default_verifier(device_ids: list[int]) -> bool:
        return True

    return TgiRuntimeProvider(
        launcher=launcher or default_launcher,
        http_client=http_client or default_http,
        lease_releaser=lease_releaser or default_releaser,
        allocation_verifier=allocation_verifier or default_verifier,
    )


def _ok_response(text: str = "hi", latency_ms: float = 50.0) -> dict[str, Any]:
    """构造成功响应 dict。"""
    return {"ok": True, "latency_ms": latency_ms, "text": text, "status": 200}


def _fail_response(latency_ms: float = 50.0) -> dict[str, Any]:
    """构造失败响应 dict。"""
    return {"ok": False, "latency_ms": latency_ms, "text": "", "status": 500}


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_deploy_starts_instance_in_running_status() -> None:
    """deploy 应启动实例并返回 RUNNING 状态与 endpoint。"""
    provider = _make_provider()
    config = _make_deploy_config(port=8080)
    alloc = _make_allocation()

    instance = _run(provider.deploy(config, alloc))

    assert isinstance(instance, TgiInstance)
    assert instance.status == InstanceStatus.RUNNING
    assert instance.endpoint == "http://localhost:8080"
    assert instance.config is config
    assert instance.allocation is alloc
    assert instance.instance_id.startswith("tgi-")


@pytest.mark.unit
def test_deploy_with_sharded_flag() -> None:
    """deploy 应支持 --sharded 多 GPU 分片模式。"""
    provider = _make_provider()
    config = _make_deploy_config(
        gpu_ids=[0, 1, 2],
        sharded=True,
    )

    instance = _run(provider.deploy(config, _make_allocation()))

    assert instance.config.sharded is True
    assert instance.config.gpu_ids == [0, 1, 2]


@pytest.mark.unit
def test_deploy_with_quantize_option() -> None:
    """deploy 应支持 --quantize 量化参数。"""
    provider = _make_provider()
    config = _make_deploy_config(quantize="bitsandbytes")

    instance = _run(provider.deploy(config, _make_allocation()))

    assert instance.config.quantize == "bitsandbytes"


@pytest.mark.unit
def test_deploy_with_gptq_quantize() -> None:
    """deploy 应支持 gptq 量化方式。"""
    provider = _make_provider()
    config = _make_deploy_config(quantize="gptq")

    instance = _run(provider.deploy(config, _make_allocation()))

    assert instance.config.quantize == "gptq"


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_verify_passes_when_ok() -> None:
    """verify 在响应 ok 时返回 passed=True。"""
    provider = _make_provider(http_client=lambda e, p: asyncio.sleep(0, result=_ok_response()))
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    result = _run(provider.verify(instance))

    assert isinstance(result, VerifyResult)
    assert result.passed is True
    assert result.response == "hi"
    assert result.latency_ms == pytest.approx(50.0)


@pytest.mark.unit
def test_verify_fails_when_not_ok() -> None:
    """verify 在响应 not ok 时返回 passed=False。"""
    provider = _make_provider(http_client=lambda e, p: asyncio.sleep(0, result=_fail_response()))
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    result = _run(provider.verify(instance))

    assert result.passed is False


@pytest.mark.unit
def test_verify_fails_on_exception() -> None:
    """verify 在 HTTP 客户端抛异常时返回 passed=False。"""

    async def failing_client(endpoint: str, prompt: str) -> dict[str, Any]:
        raise RuntimeError("connection refused")

    provider = _make_provider(http_client=failing_client)
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    result = _run(provider.verify(instance))

    assert result.passed is False
    assert "error" in result.details


@pytest.mark.unit
def test_verify_fails_when_no_endpoint() -> None:
    """verify 对无 endpoint 实例应返回 passed=False。"""
    provider = _make_provider()
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )
    bad = instance.model_copy(update={"endpoint": None})

    result = _run(provider.verify(bad))

    assert result.passed is False
    assert "endpoint" in result.details["error"]


# --------------------------------------------------------------------------- #
# release
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_release_stops_instance_and_releases_resources() -> None:
    """release 应停止实例、释放租约、核验分配。"""
    provider = _make_provider()
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    _run(provider.release(instance))

    stored = _run(provider.get_instance(instance.instance_id))
    assert stored is not None
    assert stored.status == InstanceStatus.STOPPED


@pytest.mark.unit
def test_release_raises_when_instance_not_found() -> None:
    """release 对不存在实例应抛 ValueError。"""
    provider = _make_provider()
    orphan = TgiInstance(
        instance_id="tgi-orphan",
        config=_make_deploy_config(),
        allocation=_make_allocation(),
        status=InstanceStatus.RUNNING,
        endpoint="http://localhost:9999",
    )

    with pytest.raises(ValueError, match="instance not found"):
        _run(provider.release(orphan))


@pytest.mark.unit
def test_release_raises_when_lease_release_fails() -> None:
    """租约释放失败时 release 应抛 ValueError。"""

    async def fail_releaser(lease_id: str) -> bool:
        return False

    provider = _make_provider(lease_releaser=fail_releaser)
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    with pytest.raises(ValueError, match="lease release failed"):
        _run(provider.release(instance))


@pytest.mark.unit
def test_release_raises_when_allocation_verify_fails() -> None:
    """分配核验失败时 release 应抛 ValueError。"""

    async def fail_verifier(device_ids: list[int]) -> bool:
        return False

    provider = _make_provider(allocation_verifier=fail_verifier)
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    with pytest.raises(ValueError, match="allocation verification failed"):
        _run(provider.release(instance))


# --------------------------------------------------------------------------- #
# health_check
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_health_check_returns_healthy_when_ok() -> None:
    """health_check 在响应 ok 时返回 healthy=True。"""
    provider = _make_provider(http_client=lambda e, p: asyncio.sleep(0, result=_ok_response()))
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    result = _run(provider.health_check(instance))

    assert isinstance(result, HealthStatus)
    assert result.healthy is True
    assert result.endpoint == instance.endpoint


@pytest.mark.unit
def test_health_check_returns_unhealthy_when_not_ok() -> None:
    """health_check 在响应 not ok 时返回 healthy=False。"""
    provider = _make_provider(http_client=lambda e, p: asyncio.sleep(0, result=_fail_response()))
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    result = _run(provider.health_check(instance))

    assert result.healthy is False


@pytest.mark.unit
def test_health_check_returns_unhealthy_on_exception() -> None:
    """health_check 在 HTTP 客户端抛异常时返回 healthy=False。"""

    async def failing_client(endpoint: str, prompt: str) -> dict[str, Any]:
        raise RuntimeError("timeout")

    provider = _make_provider(http_client=failing_client)
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    result = _run(provider.health_check(instance))

    assert result.healthy is False
    assert "error" in result.details


@pytest.mark.unit
def test_health_check_returns_unhealthy_when_no_endpoint() -> None:
    """health_check 对无 endpoint 实例应返回 healthy=False。"""
    provider = _make_provider()
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )
    bad = instance.model_copy(update={"endpoint": None})

    result = _run(provider.health_check(bad))

    assert result.healthy is False


# --------------------------------------------------------------------------- #
# 查询辅助
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_get_instance_returns_none_when_absent() -> None:
    """get_instance 对不存在 id 应返回 None。"""
    provider = _make_provider()
    assert _run(provider.get_instance("no-such")) is None


@pytest.mark.unit
def test_list_instances_returns_all_deployed() -> None:
    """list_instances 应返回所有已部署实例。"""
    provider = _make_provider()
    i1 = _run(provider.deploy(_make_deploy_config(port=8081), _make_allocation()))
    i2 = _run(provider.deploy(_make_deploy_config(port=8082), _make_allocation()))

    instances = _run(provider.list_instances())
    ids = {i.instance_id for i in instances}
    assert ids == {i1.instance_id, i2.instance_id}


# --------------------------------------------------------------------------- #
# 契约一致性：deploy → verify → release 闭环（与 runtime-vllm 行为一致）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_contract_deploy_verify_release_loop() -> None:
    """契约一致性：deploy → verify → release 闭环成功，与 runtime-vllm 行为一致。"""
    provider = _make_provider()
    config = _make_deploy_config(port=8090)
    alloc = _make_allocation(lease_id="lease-loop", device_ids=[0, 1])

    # 1. deploy — 实例 RUNNING 且有 endpoint。
    instance = _run(provider.deploy(config, alloc))
    assert instance.status == InstanceStatus.RUNNING
    assert instance.endpoint is not None

    # 2. verify — 冒烟通过。
    verify_result = _run(provider.verify(instance))
    assert verify_result.passed is True

    # 3. release — 实例 STOPPED。
    _run(provider.release(instance))
    stored = _run(provider.get_instance(instance.instance_id))
    assert stored is not None
    assert stored.status == InstanceStatus.STOPPED


@pytest.mark.unit
def test_contract_deploy_returns_consistent_instance_shape() -> None:
    """契约一致性：deploy 返回的实例结构完整，与 runtime-vllm 一致。"""
    provider = _make_provider()
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    # 与 vllm 的 VLLMInstance 结构对应：instance_id / config / status / endpoint / started_at。
    assert instance.instance_id.startswith("tgi-")
    assert instance.config.model_id == "meta-llama/Llama-3-8B-Instruct"
    assert instance.status == InstanceStatus.RUNNING
    assert instance.endpoint is not None
    assert instance.started_at > 0


@pytest.mark.unit
def test_contract_verify_result_shape_matches_vllm() -> None:
    """契约一致性：verify 返回的 VerifyResult 结构与 runtime-vllm 一致。"""
    provider = _make_provider()
    instance = _run(
        provider.deploy(_make_deploy_config(), _make_allocation()),
    )

    result = _run(provider.verify(instance))

    # 与 vllm 的 VerifyResult 字段对应：passed / latency_ms / response / details。
    assert hasattr(result, "passed")
    assert hasattr(result, "latency_ms")
    assert hasattr(result, "response")
    assert hasattr(result, "details")
    assert result.passed is True
