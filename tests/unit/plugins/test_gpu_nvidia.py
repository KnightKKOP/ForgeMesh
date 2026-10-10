"""gpu-nvidia 插件单元测试。

覆盖：

* **discovery** — ``discover`` 返回设备列表、``match_profile`` 匹配/不匹配、
  ``collect_capabilities`` 能力采集。
* **preflight** — ``validate_driver`` / ``validate_cuda`` / ``validate_runtime``
  通过/失败、``validate_all`` 汇总、可诊断信息。
* **health** — ``check_device`` Healthy/Degraded/Unhealthy、``check_all``、
  温度/ECC 阈值、``monitor_loop`` 周期循环。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from forgemesh.resource.node import DeviceProfile
from gpu_nvidia_plugin.discovery import DiscoveryResult, GPUDevice, GPUDiscoverer
from gpu_nvidia_plugin.health import (
    DEGRADED,
    HEALTHY,
    UNHEALTHY,
    GPUHealthChecker,
    GPUHealthStatus,
)
from gpu_nvidia_plugin.preflight import (
    PreflightResult,
    PreflightValidator,
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


def _make_device(
    *,
    index: int = 0,
    name: str = "NVIDIA H100 80GB HBM3",
    uuid: str = "GPU-00000000-0000-0000-0000-000000000000",
    memory_bytes: int = 80 * 1024**3,
    driver_version: str = "535.104.05",
    cuda_version: str = "12.2",
    compute_capability: tuple[int, int] = (9, 0),
    mig_enabled: bool = True,
) -> GPUDevice:
    """构造测试用 GPUDevice。"""
    return GPUDevice(
        index=index,
        name=name,
        uuid=uuid,
        memory_bytes=memory_bytes,
        driver_version=driver_version,
        cuda_version=cuda_version,
        compute_capability=compute_capability,
        mig_enabled=mig_enabled,
    )


def _raw_device(**overrides: Any) -> dict[str, Any]:
    """构造原始设备 dict（模拟枚举函数返回值）。"""
    base: dict[str, Any] = {
        "index": 0,
        "name": "NVIDIA H100 80GB HBM3",
        "uuid": "GPU-aaa",
        "memory_bytes": 80 * 1024**3,
        "driver_version": "535.104.05",
        "cuda_version": "12.2",
        "compute_capability": (9, 0),
        "mig_enabled": True,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# discovery — GPUDiscoverer
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_discover_returns_devices_from_enumerator() -> None:
    """discover 应将枚举函数返回的原始列表转为 GPUDevice 列表。"""

    async def mock_enumerate() -> list[dict[str, Any]]:
        return [_raw_device(index=0), _raw_device(index=1, name="NVIDIA A100")]

    discoverer = GPUDiscoverer(enumerator=mock_enumerate)
    result = _run(discoverer.discover())

    assert isinstance(result, DiscoveryResult)
    assert len(result.devices) == 2
    assert result.devices[0].index == 0
    assert result.devices[0].name == "NVIDIA H100 80GB HBM3"
    assert result.devices[1].index == 1
    assert result.devices[1].name == "NVIDIA A100"
    assert result.timestamp > 0


@pytest.mark.unit
def test_discover_builds_topology() -> None:
    """discover 应在 topology 中包含设备数与索引列表。"""

    async def mock_enumerate() -> list[dict[str, Any]]:
        return [_raw_device(index=0), _raw_device(index=1), _raw_device(index=2)]

    discoverer = GPUDiscoverer(enumerator=mock_enumerate)
    result = _run(discoverer.discover())

    assert result.topology["device_count"] == 3
    assert result.topology["device_indices"] == [0, 1, 2]
    assert result.topology["links"] == []


@pytest.mark.unit
def test_discover_empty_when_no_devices() -> None:
    """枚举返回空列表时，discover 返回空设备列表。"""

    async def mock_enumerate() -> list[dict[str, Any]]:
        return []

    discoverer = GPUDiscoverer(enumerator=mock_enumerate)
    result = _run(discoverer.discover())

    assert result.devices == []
    assert result.topology["device_count"] == 0


@pytest.mark.unit
def test_match_profile_matches_nvidia_with_sufficient_capabilities() -> None:
    """设备满足 profile 所有能力要求时应匹配。"""
    device = _make_device()
    profile = DeviceProfile(
        vendor="nvidia",
        capabilities={
            "min_memory_bytes": 40 * 1024**3,
            "min_compute_capability": [8, 0],
            "mig_required": True,
        },
    )
    discoverer = GPUDiscoverer()
    assert _run(discoverer.match_profile(device, profile)) is True


@pytest.mark.unit
def test_match_profile_rejects_non_nvidia_vendor() -> None:
    """非 nvidia vendor 应不匹配。"""
    device = _make_device()
    profile = DeviceProfile(vendor="amd", capabilities={})
    discoverer = GPUDiscoverer()
    assert _run(discoverer.match_profile(device, profile)) is False


@pytest.mark.unit
def test_match_profile_rejects_insufficient_memory() -> None:
    """显存不满足下限应不匹配。"""
    device = _make_device(memory_bytes=16 * 1024**3)
    profile = DeviceProfile(
        vendor="nvidia",
        capabilities={"min_memory_bytes": 40 * 1024**3},
    )
    discoverer = GPUDiscoverer()
    assert _run(discoverer.match_profile(device, profile)) is False


@pytest.mark.unit
def test_match_profile_rejects_insufficient_compute_capability() -> None:
    """算力不满足下限应不匹配。"""
    device = _make_device(compute_capability=(7, 5))
    profile = DeviceProfile(
        vendor="nvidia",
        capabilities={"min_compute_capability": [8, 0]},
    )
    discoverer = GPUDiscoverer()
    assert _run(discoverer.match_profile(device, profile)) is False


@pytest.mark.unit
def test_match_profile_rejects_mig_required_but_not_enabled() -> None:
    """要求 MIG 但设备未启用 MIG 应不匹配。"""
    device = _make_device(mig_enabled=False)
    profile = DeviceProfile(
        vendor="nvidia",
        capabilities={"mig_required": True},
    )
    discoverer = GPUDiscoverer()
    assert _run(discoverer.match_profile(device, profile)) is False


@pytest.mark.unit
def test_match_profile_rejects_model_mismatch() -> None:
    """型号子串不匹配应不匹配。"""
    device = _make_device(name="NVIDIA A100")
    profile = DeviceProfile(
        vendor="nvidia",
        capabilities={"model": "H100"},
    )
    discoverer = GPUDiscoverer()
    assert _run(discoverer.match_profile(device, profile)) is False


@pytest.mark.unit
def test_match_profile_empty_capabilities_matches() -> None:
    """profile 无能力约束时，nvidia 设备应匹配。"""
    device = _make_device()
    profile = DeviceProfile(vendor="nvidia", capabilities={})
    discoverer = GPUDiscoverer()
    assert _run(discoverer.match_profile(device, profile)) is True


@pytest.mark.unit
def test_collect_capabilities_returns_full_dict() -> None:
    """collect_capabilities 应返回包含所有能力字段的字典。"""
    device = _make_device(
        memory_bytes=80 * 1024**3,
        compute_capability=(9, 0),
        mig_enabled=True,
    )
    discoverer = GPUDiscoverer()
    caps = _run(discoverer.collect_capabilities(device))

    assert caps["name"] == "NVIDIA H100 80GB HBM3"
    assert caps["memory_bytes"] == 80 * 1024**3
    assert caps["memory_gib"] == pytest.approx(80.0, abs=0.01)
    assert caps["compute_capability"] == "9.0"
    assert caps["compute_capability_tuple"] == [9, 0]
    assert caps["mig_enabled"] is True
    assert caps["driver_version"] == "535.104.05"
    assert caps["cuda_version"] == "12.2"


# --------------------------------------------------------------------------- #
# preflight — PreflightValidator
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_validate_driver_passes_when_version_sufficient() -> None:
    """驱动版本满足最低要求时应通过。"""

    async def driver_info() -> dict[str, Any]:
        return {"version": "535.104.05"}

    validator = PreflightValidator(driver_info_provider=driver_info)
    check = _run(validator.validate_driver(min_version="535.0"))

    assert check.name == "driver"
    assert check.passed is True
    assert "535.104.05" in check.message
    assert check.details["actual"] == "535.104.05"
    assert check.details["min_version"] == "535.0"


@pytest.mark.unit
def test_validate_driver_fails_when_version_insufficient() -> None:
    """驱动版本不满足最低要求时应失败并携带诊断信息。"""

    async def driver_info() -> dict[str, Any]:
        return {"version": "530.0"}

    validator = PreflightValidator(driver_info_provider=driver_info)
    check = _run(validator.validate_driver(min_version="535.0"))

    assert check.passed is False
    assert "530.0" in check.message
    assert check.details["actual"] == "530.0"


@pytest.mark.unit
def test_validate_driver_fails_when_version_empty() -> None:
    """驱动版本为空（未检测到）时应失败。"""

    async def driver_info() -> dict[str, Any]:
        return {"version": ""}

    validator = PreflightValidator(driver_info_provider=driver_info)
    check = _run(validator.validate_driver())

    assert check.passed is False


@pytest.mark.unit
def test_validate_driver_fails_on_exception() -> None:
    """信息提供者抛异常时应返回 passed=False 且包含错误信息。"""

    async def driver_info() -> dict[str, Any]:
        raise RuntimeError("NVML init failed")

    validator = PreflightValidator(driver_info_provider=driver_info)
    check = _run(validator.validate_driver())

    assert check.passed is False
    assert "NVML init failed" in check.message
    assert "error" in check.details


@pytest.mark.unit
def test_validate_cuda_passes_when_version_sufficient() -> None:
    """CUDA 版本满足最低要求时应通过。"""

    async def cuda_info() -> dict[str, Any]:
        return {"version": "12.2"}

    validator = PreflightValidator(cuda_info_provider=cuda_info)
    check = _run(validator.validate_cuda(min_version="12.0"))

    assert check.name == "cuda"
    assert check.passed is True
    assert check.details["actual"] == "12.2"


@pytest.mark.unit
def test_validate_cuda_fails_when_version_insufficient() -> None:
    """CUDA 版本不满足最低要求时应失败。"""

    async def cuda_info() -> dict[str, Any]:
        return {"version": "11.8"}

    validator = PreflightValidator(cuda_info_provider=cuda_info)
    check = _run(validator.validate_cuda(min_version="12.0"))

    assert check.passed is False
    assert "11.8" in check.message


@pytest.mark.unit
def test_validate_runtime_passes_when_available() -> None:
    """运行时可用时应通过。"""

    async def runtime_info() -> dict[str, Any]:
        return {"available": True, "name": "nvidia-container-runtime", "path": "/usr/bin/..."}

    validator = PreflightValidator(runtime_info_provider=runtime_info)
    check = _run(validator.validate_runtime())

    assert check.name == "runtime"
    assert check.passed is True
    assert "nvidia-container-runtime" in check.message


@pytest.mark.unit
def test_validate_runtime_fails_when_unavailable() -> None:
    """运行时不可用时应失败。"""

    async def runtime_info() -> dict[str, Any]:
        return {"available": False, "name": "nvidia-container-runtime"}

    validator = PreflightValidator(runtime_info_provider=runtime_info)
    check = _run(validator.validate_runtime())

    assert check.passed is False
    assert "不可用" in check.message


@pytest.mark.unit
def test_validate_all_all_passed() -> None:
    """全部检查通过时 all_passed=True。"""

    async def driver_info() -> dict[str, Any]:
        return {"version": "535.104.05"}

    async def cuda_info() -> dict[str, Any]:
        return {"version": "12.2"}

    async def runtime_info() -> dict[str, Any]:
        return {"available": True, "name": "nvidia-container-runtime"}

    validator = PreflightValidator(
        driver_info_provider=driver_info,
        cuda_info_provider=cuda_info,
        runtime_info_provider=runtime_info,
    )
    result = _run(validator.validate_all())

    assert isinstance(result, PreflightResult)
    assert result.all_passed is True
    assert len(result.checks) == 3
    assert "3/3" in result.summary


@pytest.mark.unit
def test_validate_all_partial_failure() -> None:
    """部分检查失败时 all_passed=False 且 summary 反映通过数。"""

    async def driver_info() -> dict[str, Any]:
        return {"version": "535.104.05"}

    async def cuda_info() -> dict[str, Any]:
        return {"version": "11.8"}  # 不满足 12.0

    async def runtime_info() -> dict[str, Any]:
        return {"available": True, "name": "nvidia-container-runtime"}

    validator = PreflightValidator(
        driver_info_provider=driver_info,
        cuda_info_provider=cuda_info,
        runtime_info_provider=runtime_info,
    )
    result = _run(validator.validate_all())

    assert result.all_passed is False
    assert "2/3" in result.summary
    cuda_check = next(c for c in result.checks if c.name == "cuda")
    assert cuda_check.passed is False


# --------------------------------------------------------------------------- #
# health — GPUHealthChecker
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_check_device_healthy_when_within_thresholds() -> None:
    """温度与 ECC 均在阈值内时状态为 Healthy。"""

    async def check_fn(idx: int) -> dict[str, Any]:
        return {
            "temperature": 70.0,
            "utilization": 50.0,
            "memory_used": 40 * 1024**3,
            "memory_total": 80 * 1024**3,
            "ecc_errors": 0,
        }

    checker = GPUHealthChecker(device_count=1, check_fn=check_fn)
    status = _run(checker.check_device(0))

    assert status.device_index == 0
    assert status.status == HEALTHY
    assert status.temperature == 70.0
    assert status.utilization == 50.0
    assert status.ecc_errors == 0


@pytest.mark.unit
def test_check_device_degraded_when_temperature_exceeds_threshold() -> None:
    """温度超阈值时状态为 Degraded。"""

    async def check_fn(idx: int) -> dict[str, Any]:
        return {"temperature": 90.0, "ecc_errors": 0}

    checker = GPUHealthChecker(device_count=1, check_fn=check_fn, temp_threshold=85.0)
    status = _run(checker.check_device(0))

    assert status.status == DEGRADED
    assert status.temperature == 90.0


@pytest.mark.unit
def test_check_device_unhealthy_when_ecc_exceeds_threshold() -> None:
    """ECC 错误超阈值时状态为 Unhealthy（优先于温度）。"""

    async def check_fn(idx: int) -> dict[str, Any]:
        return {"temperature": 90.0, "ecc_errors": 20}

    checker = GPUHealthChecker(
        device_count=1,
        check_fn=check_fn,
        temp_threshold=85.0,
        ecc_threshold=10,
    )
    status = _run(checker.check_device(0))

    assert status.status == UNHEALTHY
    assert status.ecc_errors == 20


@pytest.mark.unit
def test_check_device_unhealthy_on_exception() -> None:
    """检查函数抛异常时状态为 Unhealthy。"""

    async def check_fn(idx: int) -> dict[str, Any]:
        raise RuntimeError("NVML error")

    checker = GPUHealthChecker(device_count=1, check_fn=check_fn)
    status = _run(checker.check_device(0))

    assert status.status == UNHEALTHY


@pytest.mark.unit
def test_check_device_uses_custom_thresholds() -> None:
    """自定义阈值应生效：温度 80 在阈值 80 下为 Healthy（不严格大于）。"""

    async def check_fn(idx: int) -> dict[str, Any]:
        return {"temperature": 80.0, "ecc_errors": 5}

    checker = GPUHealthChecker(
        device_count=1,
        check_fn=check_fn,
        temp_threshold=80.0,
        ecc_threshold=5,
    )
    status = _run(checker.check_device(0))

    # 80.0 > 80.0 → False，5 > 5 → False
    assert status.status == HEALTHY


@pytest.mark.unit
def test_check_all_returns_all_devices() -> None:
    """check_all 应返回 device_count 个设备状态。"""

    async def check_fn(idx: int) -> dict[str, Any]:
        return {"temperature": 50.0 + idx, "ecc_errors": 0}

    checker = GPUHealthChecker(device_count=3, check_fn=check_fn)
    statuses = _run(checker.check_all())

    assert len(statuses) == 3
    assert [s.device_index for s in statuses] == [0, 1, 2]
    assert all(s.status == HEALTHY for s in statuses)


@pytest.mark.unit
def test_check_all_empty_when_no_devices() -> None:
    """device_count=0 时 check_all 返回空列表。"""
    checker = GPUHealthChecker(device_count=0)
    statuses = _run(checker.check_all())
    assert statuses == []


@pytest.mark.unit
def test_monitor_loop_invokes_callback_each_iteration() -> None:
    """monitor_loop 应每轮调用 callback 并传入状态列表。"""
    call_count = 0
    received: list[list[GPUHealthStatus]] = []

    async def check_fn(idx: int) -> dict[str, Any]:
        return {"temperature": 60.0, "ecc_errors": 0}

    async def callback(statuses: list[GPUHealthStatus]) -> None:
        nonlocal call_count
        call_count += 1
        received.append(statuses)

    checker = GPUHealthChecker(device_count=2, check_fn=check_fn)
    _run(checker.monitor_loop(interval=0.01, callback=callback, max_iterations=3))

    assert call_count == 3
    assert all(len(batch) == 2 for batch in received)
    assert all(s.status == HEALTHY for batch in received for s in batch)


@pytest.mark.unit
def test_monitor_loop_without_callback() -> None:
    """monitor_loop 无 callback 时应正常运行（仅采集）。"""

    async def check_fn(idx: int) -> dict[str, Any]:
        return {"temperature": 60.0, "ecc_errors": 0}

    checker = GPUHealthChecker(device_count=1, check_fn=check_fn)
    # 不抛异常即通过
    _run(checker.monitor_loop(interval=0.01, callback=None, max_iterations=2))


@pytest.mark.unit
def test_monitor_loop_reports_degraded_status() -> None:
    """monitor_loop 应上报 Degraded 状态（温度超阈值）。"""
    received: list[GPUHealthStatus] = []

    async def check_fn(idx: int) -> dict[str, Any]:
        return {"temperature": 95.0, "ecc_errors": 0}

    async def callback(statuses: list[GPUHealthStatus]) -> None:
        received.extend(statuses)

    checker = GPUHealthChecker(
        device_count=1,
        check_fn=check_fn,
        temp_threshold=85.0,
    )
    _run(checker.monitor_loop(interval=0.01, callback=callback, max_iterations=1))

    assert len(received) == 1
    assert received[0].status == DEGRADED
