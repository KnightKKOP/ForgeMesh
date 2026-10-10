"""runtime-vllm 插件单元测试。

覆盖：

* **model** — ``fetch`` / ``verify_digest``（匹配/不匹配抛异常）/ ``prepare`` 完整流程。
* **deploy** — ``deploy`` 启动实例 / ``register_route`` / ``get_instance`` /
  ``list_instances``。
* **verify** — ``smoke_test`` 通过/失败 / ``measure_latency`` /
  ``measure_error_rate`` / ``verify``（延迟超限/错误率超限/全部通过）。
* **release** — ``stop_instance`` / ``release_lease`` / ``verify_allocation`` /
  ``release`` 完整流程。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from runtime_vllm_plugin.deploy import (
    DeployConfig,
    InstanceStatus,
    VLLMDeployer,
    VLLMInstance,
)
from runtime_vllm_plugin.model import ModelArtifact, ModelPreparer
from runtime_vllm_plugin.release import ReleaseResult, ResourceReleaser
from runtime_vllm_plugin.verify import (
    ServiceVerifier,
    VerifyConfig,
    VerifyResult,
)


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


def _run(coro: Any) -> Any:
    """在当前事件循环上运行协程并返回结果。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_artifact(
    *,
    name: str = "llama-3-8b",
    version: str = "1.0.0",
    uri: str = "hf://meta-llama/Llama-3-8B",
    digest: str = "sha256:abc123",
    size_bytes: int = 8 * 1024**3,
) -> ModelArtifact:
    """构造测试用 ModelArtifact。"""
    return ModelArtifact(
        name=name,
        version=version,
        uri=uri,
        digest=digest,
        size_bytes=size_bytes,
    )


def _make_deploy_config(
    *,
    model_path: str = "/models/llama-3-8b",
    port: int = 8000,
    gpu_ids: list[int] | None = None,
    tensor_parallel_size: int = 1,
) -> DeployConfig:
    """构造测试用 DeployConfig。"""
    return DeployConfig(
        model_path=model_path,
        port=port,
        gpu_ids=gpu_ids if gpu_ids is not None else [0],
        tensor_parallel_size=tensor_parallel_size,
    )


# --------------------------------------------------------------------------- #
# model — ModelPreparer
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_fetch_returns_artifact_from_fetcher() -> None:
    """fetch 应返回注入 fetcher 产出的 ModelArtifact。"""

    async def mock_fetcher(uri: str) -> ModelArtifact:
        return _make_artifact(uri=uri)

    preparer = ModelPreparer(fetcher=mock_fetcher)
    artifact = _run(preparer.fetch("hf://meta-llama/Llama-3-8B"))

    assert artifact.name == "llama-3-8b"
    assert artifact.uri == "hf://meta-llama/Llama-3-8B"
    assert artifact.digest == "sha256:abc123"


@pytest.mark.unit
def test_verify_digest_returns_true_when_matched() -> None:
    """摘要匹配时 verify_digest 返回 True。"""
    preparer = ModelPreparer()
    artifact = _make_artifact(digest="sha256:abc123")

    assert _run(preparer.verify_digest(artifact, "sha256:abc123")) is True


@pytest.mark.unit
def test_verify_digest_raises_on_mismatch() -> None:
    """摘要不匹配时 verify_digest 抛 ValueError。"""
    preparer = ModelPreparer()
    artifact = _make_artifact(digest="sha256:abc123")

    with pytest.raises(ValueError, match="digest mismatch"):
        _run(preparer.verify_digest(artifact, "sha256:different"))


@pytest.mark.unit
def test_prepare_runs_fetch_then_verify() -> None:
    """prepare 应依次执行 fetch 与 verify_digest，返回校验通过的制品。"""
    fetched = _make_artifact(digest="sha256:abc123")

    async def mock_fetcher(uri: str) -> ModelArtifact:
        return fetched

    preparer = ModelPreparer(fetcher=mock_fetcher)
    artifact = _run(preparer.prepare("hf://meta-llama/Llama-3-8B", "sha256:abc123"))

    assert artifact is fetched


@pytest.mark.unit
def test_prepare_raises_when_digest_mismatch() -> None:
    """prepare 在摘要不匹配时应抛 ValueError。"""

    async def mock_fetcher(uri: str) -> ModelArtifact:
        return _make_artifact(digest="sha256:abc123")

    preparer = ModelPreparer(fetcher=mock_fetcher)
    with pytest.raises(ValueError, match="digest mismatch"):
        _run(preparer.prepare("hf://meta-llama/Llama-3-8B", "sha256:wrong"))


# --------------------------------------------------------------------------- #
# deploy — VLLMDeployer
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_deploy_starts_instance_in_running_status() -> None:
    """deploy 应启动实例并返回 RUNNING 状态与 endpoint。"""

    async def mock_launcher(config: DeployConfig, instance_id: str) -> str:
        return f"http://localhost:{config.port}"

    deployer = VLLMDeployer(launcher=mock_launcher)
    config = _make_deploy_config(port=8000)
    instance = _run(deployer.deploy(config))

    assert isinstance(instance, VLLMInstance)
    assert instance.status == InstanceStatus.RUNNING
    assert instance.endpoint == "http://localhost:8000"
    assert instance.config is config
    assert instance.instance_id.startswith("vllm-")


@pytest.mark.unit
def test_register_route_returns_endpoint_and_marks_ready() -> None:
    """register_route 应返回 endpoint 并将实例状态推进为 READY。"""

    async def mock_launcher(config: DeployConfig, instance_id: str) -> str:
        return f"http://localhost:{config.port}"

    deployer = VLLMDeployer(launcher=mock_launcher)
    instance = _run(deployer.deploy(_make_deploy_config(port=8001)))
    endpoint = _run(deployer.register_route(instance))

    assert endpoint == "http://localhost:8001"
    stored = _run(deployer.get_instance(instance.instance_id))
    assert stored is not None
    assert stored.status == InstanceStatus.READY


@pytest.mark.unit
def test_register_route_raises_when_instance_not_found() -> None:
    """register_route 对未部署实例应抛 ValueError。"""

    async def mock_launcher(config: DeployConfig, instance_id: str) -> str:
        return f"http://localhost:{config.port}"

    deployer = VLLMDeployer(launcher=mock_launcher)
    orphan = VLLMInstance(
        instance_id="vllm-orphan",
        config=_make_deploy_config(),
        status=InstanceStatus.RUNNING,
        endpoint="http://localhost:9999",
    )
    with pytest.raises(ValueError, match="instance not found"):
        _run(deployer.register_route(orphan))


@pytest.mark.unit
def test_register_route_raises_when_no_endpoint() -> None:
    """register_route 对无 endpoint 实例应抛 ValueError。"""

    async def mock_launcher(config: DeployConfig, instance_id: str) -> str:
        return f"http://localhost:{config.port}"

    deployer = VLLMDeployer(launcher=mock_launcher)
    instance = _run(deployer.deploy(_make_deploy_config()))
    # 篡改 endpoint 为空以触发校验。
    bad = instance.model_copy(update={"endpoint": None})
    with pytest.raises(ValueError, match="no endpoint"):
        _run(deployer.register_route(bad))


@pytest.mark.unit
def test_get_instance_returns_none_when_absent() -> None:
    """get_instance 对不存在 id 应返回 None。"""
    deployer = VLLMDeployer(launcher=lambda c, i: asyncio.sleep(0))  # noqa: E731
    assert _run(deployer.get_instance("no-such")) is None


@pytest.mark.unit
def test_list_instances_returns_all_deployed() -> None:
    """list_instances 应返回所有已部署实例。"""

    async def mock_launcher(config: DeployConfig, instance_id: str) -> str:
        return f"http://localhost:{config.port}"

    deployer = VLLMDeployer(launcher=mock_launcher)
    i1 = _run(deployer.deploy(_make_deploy_config(port=8001)))
    i2 = _run(deployer.deploy(_make_deploy_config(port=8002)))

    instances = _run(deployer.list_instances())
    ids = {i.instance_id for i in instances}
    assert ids == {i1.instance_id, i2.instance_id}


# --------------------------------------------------------------------------- #
# verify — ServiceVerifier
# --------------------------------------------------------------------------- #


def _ok_response(text: str = "hi", latency_ms: float = 100.0) -> dict[str, Any]:
    """构造成功响应 dict。"""
    return {"ok": True, "latency_ms": latency_ms, "text": text, "status": 200}


def _fail_response(latency_ms: float = 100.0) -> dict[str, Any]:
    """构造失败响应 dict。"""
    return {"ok": False, "latency_ms": latency_ms, "text": "", "status": 500}


@pytest.mark.unit
def test_smoke_test_passes_when_ok() -> None:
    """smoke_test 在响应 ok 时返回 True。"""

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        return _ok_response()

    verifier = ServiceVerifier(http_client=client)
    assert _run(verifier.smoke_test("http://x", "ping")) is True


@pytest.mark.unit
def test_smoke_test_fails_when_not_ok() -> None:
    """smoke_test 在响应 not ok 时返回 False。"""

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        return _fail_response()

    verifier = ServiceVerifier(http_client=client)
    assert _run(verifier.smoke_test("http://x", "ping")) is False


@pytest.mark.unit
def test_smoke_test_fails_on_exception() -> None:
    """smoke_test 在客户端抛异常时返回 False。"""

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        raise RuntimeError("conn refused")

    verifier = ServiceVerifier(http_client=client)
    assert _run(verifier.smoke_test("http://x", "ping")) is False


@pytest.mark.unit
def test_measure_latency_returns_average() -> None:
    """measure_latency 应返回多次请求的平均延迟。"""
    latencies = [100.0, 200.0, 300.0]
    call_count = {"n": 0}

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        idx = call_count["n"] % len(latencies)
        call_count["n"] += 1
        return _ok_response(latency_ms=latencies[idx])

    verifier = ServiceVerifier(http_client=client)
    avg = _run(verifier.measure_latency("http://x", 3))

    assert avg == pytest.approx(200.0)


@pytest.mark.unit
def test_measure_latency_zero_when_no_requests() -> None:
    """measure_latency 在 num_requests=0 时返回 0。"""

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        return _ok_response()

    verifier = ServiceVerifier(http_client=client)
    assert _run(verifier.measure_latency("http://x", 0)) == 0.0


@pytest.mark.unit
def test_measure_error_rate_returns_failure_ratio() -> None:
    """measure_error_rate 应返回失败占比。"""
    # 5 次请求，2 次失败 → 0.4
    pattern = [True, True, True, False, False]
    call_count = {"n": 0}

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        idx = call_count["n"] % len(pattern)
        call_count["n"] += 1
        return _ok_response() if pattern[idx] else _fail_response()

    verifier = ServiceVerifier(http_client=client)
    rate = _run(verifier.measure_error_rate("http://x", 5))

    assert rate == pytest.approx(0.4)


@pytest.mark.unit
def test_measure_error_rate_counts_exceptions_as_failures() -> None:
    """measure_error_rate 应将异常计为失败。"""
    call_count = {"n": 0}

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        call_count["n"] += 1
        raise RuntimeError("boom")

    verifier = ServiceVerifier(http_client=client)
    rate = _run(verifier.measure_error_rate("http://x", 4))

    assert rate == pytest.approx(1.0)


@pytest.mark.unit
def test_verify_all_pass() -> None:
    """延迟与错误率均在阈值内时 passed=True。"""

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        return _ok_response(text="hello", latency_ms=100.0)

    verifier = ServiceVerifier(http_client=client)
    config = VerifyConfig(
        endpoint="http://x",
        test_prompt="ping",
        max_latency_ms=200.0,
        max_error_rate=0.1,
        num_requests=3,
    )
    result = _run(verifier.verify(config))

    assert isinstance(result, VerifyResult)
    assert result.passed is True
    assert result.latency_ms == pytest.approx(100.0)
    assert result.error_rate == pytest.approx(0.0)
    assert result.response == "hello"
    assert result.details["smoke_ok"] is True


@pytest.mark.unit
def test_verify_fails_when_latency_exceeds_threshold() -> None:
    """平均延迟超阈值时 passed=False。"""

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        return _ok_response(text="hello", latency_ms=500.0)

    verifier = ServiceVerifier(http_client=client)
    config = VerifyConfig(
        endpoint="http://x",
        max_latency_ms=200.0,
        max_error_rate=0.1,
        num_requests=2,
    )
    result = _run(verifier.verify(config))

    assert result.passed is False
    assert result.latency_ms == pytest.approx(500.0)
    assert result.details["latency_ok"] is False


@pytest.mark.unit
def test_verify_fails_when_error_rate_exceeds_threshold() -> None:
    """错误率超阈值时 passed=False。"""
    pattern = [True, False]  # 50% 错误率
    call_count = {"n": 0}

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        idx = call_count["n"] % len(pattern)
        call_count["n"] += 1
        return _ok_response(latency_ms=100.0) if pattern[idx] else _fail_response()

    verifier = ServiceVerifier(http_client=client)
    config = VerifyConfig(
        endpoint="http://x",
        max_latency_ms=200.0,
        max_error_rate=0.1,
        num_requests=2,
    )
    result = _run(verifier.verify(config))

    assert result.passed is False
    assert result.error_rate == pytest.approx(0.5)
    assert result.details["error_ok"] is False


@pytest.mark.unit
def test_verify_fails_when_smoke_fails() -> None:
    """冒烟推理失败时 passed=False。"""

    async def client(endpoint: str, prompt: str) -> dict[str, Any]:
        return _fail_response()

    verifier = ServiceVerifier(http_client=client)
    config = VerifyConfig(
        endpoint="http://x",
        max_latency_ms=200.0,
        max_error_rate=0.1,
        num_requests=2,
    )
    result = _run(verifier.verify(config))

    assert result.passed is False
    assert result.details["smoke_ok"] is False


# --------------------------------------------------------------------------- #
# release — ResourceReleaser
# --------------------------------------------------------------------------- #


def _make_deployer_with_instance(port: int = 8000) -> tuple[VLLMDeployer, VLLMInstance]:
    """构造已部署一个实例的 deployer，返回 (deployer, instance)。"""

    async def mock_launcher(config: DeployConfig, instance_id: str) -> str:
        return f"http://localhost:{config.port}"

    deployer = VLLMDeployer(launcher=mock_launcher)
    instance = _run(deployer.deploy(_make_deploy_config(port=port)))
    return deployer, instance


@pytest.mark.unit
def test_stop_instance_releases_running_instance() -> None:
    """stop_instance 应停止实例并返回 released=True。"""
    deployer, instance = _make_deployer_with_instance()
    releaser = ResourceReleaser(
        lease_releaser=lambda lid: asyncio.sleep(0, result=True),  # noqa: E731
        allocation_verifier=lambda ids: asyncio.sleep(0, result=True),  # noqa: E731
    )

    result = _run(releaser.stop_instance(deployer, instance.instance_id))

    assert isinstance(result, ReleaseResult)
    assert result.released is True
    assert result.instance_id == instance.instance_id
    stopped = _run(deployer.get_instance(instance.instance_id))
    assert stopped is not None
    assert stopped.status == InstanceStatus.STOPPED


@pytest.mark.unit
def test_stop_instance_returns_not_found_for_absent() -> None:
    """stop_instance 对不存在实例应返回 released=False。"""
    deployer, _ = _make_deployer_with_instance()
    releaser = ResourceReleaser()

    result = _run(releaser.stop_instance(deployer, "no-such"))

    assert result.released is False
    assert "not found" in result.details


@pytest.mark.unit
def test_release_lease_returns_true_on_success() -> None:
    """release_lease 在注入 releaser 返回 True 时应返回 True。"""

    async def mock_releaser(lease_id: str) -> bool:
        return lease_id == "lease-1"

    releaser = ResourceReleaser(lease_releaser=mock_releaser)
    assert _run(releaser.release_lease("lease-1")) is True
    assert _run(releaser.release_lease("lease-2")) is False


@pytest.mark.unit
def test_verify_allocation_returns_true_when_devices_released() -> None:
    """verify_allocation 在注入 verifier 返回 True 时应返回 True。"""

    async def mock_verifier(device_ids: list[int]) -> bool:
        return len(device_ids) > 0

    releaser = ResourceReleaser(allocation_verifier=mock_verifier)
    assert _run(releaser.verify_allocation([0, 1])) is True
    assert _run(releaser.verify_allocation([])) is False


@pytest.mark.unit
def test_release_full_flow_all_succeed() -> None:
    """完整回收流程：停止实例 + 释放租约 + 分配核验均成功。"""
    deployer, instance = _make_deployer_with_instance()

    async def mock_releaser(lease_id: str) -> bool:
        return True

    async def mock_verifier(device_ids: list[int]) -> bool:
        return True

    releaser = ResourceReleaser(
        lease_releaser=mock_releaser,
        allocation_verifier=mock_verifier,
    )
    result = _run(
        releaser.release(deployer, instance.instance_id, "lease-1", [0, 1]),
    )

    assert result.released is True
    assert result.verified is True
    assert "instance_stopped=True" in result.details
    assert "lease_released=True" in result.details
    assert "allocation_verified=True" in result.details


@pytest.mark.unit
def test_release_fails_when_instance_not_found() -> None:
    """实例不存在时 release 应返回 released=False。"""
    deployer, _ = _make_deployer_with_instance()
    releaser = ResourceReleaser(
        lease_releaser=lambda lid: asyncio.sleep(0, result=True),  # noqa: E731
        allocation_verifier=lambda ids: asyncio.sleep(0, result=True),  # noqa: E731
    )

    result = _run(releaser.release(deployer, "no-such", "lease-1", [0]))

    assert result.released is False
    assert "stop instance failed" in result.details


@pytest.mark.unit
def test_release_fails_when_lease_release_fails() -> None:
    """租约释放失败时 released=False 但实例已停止。"""
    deployer, instance = _make_deployer_with_instance()

    async def mock_releaser(lease_id: str) -> bool:
        return False

    async def mock_verifier(device_ids: list[int]) -> bool:
        return True

    releaser = ResourceReleaser(
        lease_releaser=mock_releaser,
        allocation_verifier=mock_verifier,
    )
    result = _run(
        releaser.release(deployer, instance.instance_id, "lease-1", [0]),
    )

    assert result.released is False
    assert result.verified is True
    assert "lease_released=False" in result.details


@pytest.mark.unit
def test_release_verified_false_when_allocation_check_fails() -> None:
    """分配核验失败时 verified=False。"""
    deployer, instance = _make_deployer_with_instance()

    async def mock_releaser(lease_id: str) -> bool:
        return True

    async def mock_verifier(device_ids: list[int]) -> bool:
        return False

    releaser = ResourceReleaser(
        lease_releaser=mock_releaser,
        allocation_verifier=mock_verifier,
    )
    result = _run(
        releaser.release(deployer, instance.instance_id, "lease-1", [0]),
    )

    # 实例停止且租约释放成功 → released=True；但核验失败 → verified=False。
    assert result.released is True
    assert result.verified is False
    assert "allocation_verified=False" in result.details
