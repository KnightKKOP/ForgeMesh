"""MIG (Multi-Instance GPU) 实例管理单元测试。

覆盖：

* **create_instance** — 创建实例、字段正确性、状态转移、资源校验。
* **destroy_instance** — 销毁、状态转移、不存在 / 已销毁拒绝。
* **list_instances** — 列举、按 GPU 过滤、已销毁不含。
* **discover_profiles** — profile 发现与未知名称跳过。
* **health_check** — Healthy / Degraded、异常降级、不存在 / 已销毁拒绝。
* **get_capability_report** — 显存 / 切片总量、已用、可用。
* **状态机** — 非法转移被拒。
* **并发互斥** — 同 GPU 并发创建不超分。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from gpu_nvidia_plugin.mig import (
    CREATED,
    DEGRADED,
    DESTROYED,
    HEALTHY,
    MIG_PROFILES,
    MigCapabilityReport,
    MigHealthStatus,
    MigInstance,
    MigManager,
    MigNotFoundError,
    MigProfile,
    MigResourceError,
    MigStateError,
)


@pytest.fixture(autouse=True)
def _fresh_event_loop() -> Any:
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


def _cap_provider(total_memory_gb: int = 80, total_compute_slices: int = 7) -> Any:
    """构造 GPU 能力提供者 mock。"""

    async def provider(gpu_id: int) -> dict[str, Any]:
        await asyncio.sleep(0)  # 让出控制权，触发真实并发竞争
        return {
            "total_memory_gb": total_memory_gb,
            "total_compute_slices": total_compute_slices,
        }

    return provider


# --------------------------------------------------------------------------- #
# create_instance
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_create_instance_returns_instance_with_correct_fields() -> None:
    """create_instance 应返回字段完整的 MigInstance，状态为 Created。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    profile = MIG_PROFILES["1g.5gb"]

    instance = _run(manager.create_instance(0, profile, "tenant-a"))

    assert isinstance(instance, MigInstance)
    assert instance.gpu_id == 0
    assert instance.profile == profile
    assert instance.tenant_id == "tenant-a"
    assert instance.instance_id.startswith("mig-")
    assert "gpu0:slice" in instance.placement
    # 状态应为 Created
    assert manager._states[instance.instance_id] == CREATED


@pytest.mark.unit
def test_create_instance_placement_reflects_slice_range() -> None:
    """placement 应反映计算切片起止范围。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    p1 = MIG_PROFILES["3g.20gb"]  # 3 切片 -> slice0-2
    p2 = MIG_PROFILES["2g.10gb"]  # 2 切片 -> slice3-4

    inst1 = _run(manager.create_instance(0, p1, "t1"))
    inst2 = _run(manager.create_instance(0, p2, "t2"))

    assert inst1.placement == "gpu0:slice0-2"
    assert inst2.placement == "gpu0:slice3-4"


@pytest.mark.unit
def test_create_instance_resource_exhaustion_slices() -> None:
    """计算切片耗尽时应抛 MigResourceError。"""
    manager = MigManager(gpu_capability_provider=_cap_provider(total_compute_slices=7))
    profile = MIG_PROFILES["7g.40gb"]  # 7 切片，一次占满

    _run(manager.create_instance(0, profile, "t1"))
    # 再创建任何 profile 都应因切片不足而失败
    with pytest.raises(MigResourceError, match="计算切片不足"):
        _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t2"))


@pytest.mark.unit
def test_create_instance_resource_exhaustion_memory() -> None:
    """显存耗尽时应抛 MigResourceError。"""
    manager = MigManager(
        gpu_capability_provider=_cap_provider(total_memory_gb=10, total_compute_slices=7),
    )
    profile = MIG_PROFILES["1g.10gb"]  # 10GB，一次占满

    _run(manager.create_instance(0, profile, "t1"))
    with pytest.raises(MigResourceError, match="显存不足"):
        _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t2"))


@pytest.mark.unit
def test_create_instance_on_different_gpus_are_independent() -> None:
    """不同 GPU 上的实例创建互不影响资源计数。"""
    manager = MigManager(gpu_capability_provider=_cap_provider(total_compute_slices=2))

    inst0 = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t0"))
    inst1 = _run(manager.create_instance(1, MIG_PROFILES["1g.5gb"], "t1"))

    assert inst0.gpu_id == 0
    assert inst1.gpu_id == 1
    # 各 GPU 独立计数，不应超限
    assert len(_run(manager.list_instances())) == 2


# --------------------------------------------------------------------------- #
# destroy_instance
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_destroy_instance_transitions_to_destroyed() -> None:
    """destroy_instance 应将状态转移至 Destroyed。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))

    _run(manager.destroy_instance(instance.instance_id))

    assert manager._states[instance.instance_id] == DESTROYED


@pytest.mark.unit
def test_destroy_instance_releases_resources() -> None:
    """销毁实例后资源应释放，可重新创建。"""
    manager = MigManager(
        gpu_capability_provider=_cap_provider(total_memory_gb=5, total_compute_slices=1),
    )
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))
    _run(manager.destroy_instance(instance.instance_id))

    # 资源释放后可再次创建
    again = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t2"))
    assert again.instance_id != instance.instance_id


@pytest.mark.unit
def test_destroy_nonexistent_raises_not_found() -> None:
    """销毁不存在的实例应抛 MigNotFoundError。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    with pytest.raises(MigNotFoundError):
        _run(manager.destroy_instance("mig-nonexistent"))


@pytest.mark.unit
def test_destroy_already_destroyed_raises_state_error() -> None:
    """销毁已销毁的实例应抛 MigStateError（非法状态转移）。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))
    _run(manager.destroy_instance(instance.instance_id))

    with pytest.raises(MigStateError, match="非法状态转移"):
        _run(manager.destroy_instance(instance.instance_id))


# --------------------------------------------------------------------------- #
# list_instances
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_list_instances_returns_active_only() -> None:
    """list_instances 应仅返回活跃实例，已销毁的不含。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    i1 = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))
    _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t2"))
    _run(manager.destroy_instance(i1.instance_id))

    result = _run(manager.list_instances())
    assert len(result) == 1
    assert result[0].tenant_id == "t2"


@pytest.mark.unit
def test_list_instances_filter_by_gpu() -> None:
    """list_instances(gpu_id) 应仅返回指定 GPU 上的实例。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t0"))
    _run(manager.create_instance(1, MIG_PROFILES["1g.5gb"], "t1"))
    _run(manager.create_instance(1, MIG_PROFILES["1g.5gb"], "t1b"))

    gpu0 = _run(manager.list_instances(gpu_id=0))
    gpu1 = _run(manager.list_instances(gpu_id=1))
    all_instances = _run(manager.list_instances())

    assert len(gpu0) == 1
    assert len(gpu1) == 2
    assert len(all_instances) == 3


@pytest.mark.unit
def test_list_instances_empty_when_none() -> None:
    """无实例时 list_instances 返回空列表。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    assert _run(manager.list_instances()) == []
    assert _run(manager.list_instances(gpu_id=0)) == []


# --------------------------------------------------------------------------- #
# discover_profiles
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_discover_profiles_returns_known_profiles() -> None:
    """discover_profiles 应返回映射后的 MigProfile 列表。"""

    async def discovery_fn(gpu_id: int) -> list[str]:
        return ["1g.5gb", "2g.10gb", "7g.40gb"]

    manager = MigManager(
        gpu_capability_provider=_cap_provider(),
        profile_discovery_fn=discovery_fn,
    )
    profiles = _run(manager.discover_profiles(0))

    assert len(profiles) == 3
    assert all(isinstance(p, MigProfile) for p in profiles)
    assert {p.name for p in profiles} == {"1g.5gb", "2g.10gb", "7g.40gb"}


@pytest.mark.unit
def test_discover_profiles_skips_unknown_names() -> None:
    """未知 profile 名称应被跳过，不抛异常。"""

    async def discovery_fn(gpu_id: int) -> list[str]:
        return ["1g.5gb", "unknown.profile", "2g.10gb"]

    manager = MigManager(
        gpu_capability_provider=_cap_provider(),
        profile_discovery_fn=discovery_fn,
    )
    profiles = _run(manager.discover_profiles(0))

    assert len(profiles) == 2
    assert {p.name for p in profiles} == {"1g.5gb", "2g.10gb"}


@pytest.mark.unit
def test_discover_profiles_empty() -> None:
    """发现函数返回空列表时，discover_profiles 返回空列表。"""

    async def discovery_fn(gpu_id: int) -> list[str]:
        return []

    manager = MigManager(
        gpu_capability_provider=_cap_provider(),
        profile_discovery_fn=discovery_fn,
    )
    assert _run(manager.discover_profiles(0)) == []


# --------------------------------------------------------------------------- #
# health_check
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_health_check_returns_healthy() -> None:
    """健康检查返回 Healthy 时状态应转移至 Healthy。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))

    async def check_fn(inst_id: str) -> dict[str, Any]:
        return {"status": HEALTHY, "utilization": 30.0}

    manager._health_check_fn = check_fn  # 直接注入便于测试
    status = _run(manager.health_check(instance.instance_id))

    assert isinstance(status, MigHealthStatus)
    assert status.status == HEALTHY
    assert status.instance_id == instance.instance_id
    assert status.gpu_id == 0
    assert manager._states[instance.instance_id] == HEALTHY


@pytest.mark.unit
def test_health_check_returns_degraded() -> None:
    """健康检查返回 Degraded 时状态应转移至 Degraded。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))

    async def check_fn(inst_id: str) -> dict[str, Any]:
        return {"status": DEGRADED, "reason": "high_temp"}

    manager._health_check_fn = check_fn
    status = _run(manager.health_check(instance.instance_id))

    assert status.status == DEGRADED
    assert manager._states[instance.instance_id] == DEGRADED


@pytest.mark.unit
def test_health_check_unknown_status_defaults_to_degraded() -> None:
    """健康检查返回未知状态时应默认降级为 Degraded。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))

    async def check_fn(inst_id: str) -> dict[str, Any]:
        return {"status": "Unknown"}

    manager._health_check_fn = check_fn
    status = _run(manager.health_check(instance.instance_id))

    assert status.status == DEGRADED


@pytest.mark.unit
def test_health_check_exception_degrades_instance() -> None:
    """检查函数抛异常时实例应降级为 Degraded。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))

    async def check_fn(inst_id: str) -> dict[str, Any]:
        raise RuntimeError("NVML error")

    manager._health_check_fn = check_fn
    status = _run(manager.health_check(instance.instance_id))

    assert status.status == DEGRADED
    assert "NVML error" in status.details["error"]
    assert manager._states[instance.instance_id] == DEGRADED


@pytest.mark.unit
def test_health_check_nonexistent_raises_not_found() -> None:
    """健康检查不存在的实例应抛 MigNotFoundError。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    with pytest.raises(MigNotFoundError):
        _run(manager.health_check("mig-nonexistent"))


@pytest.mark.unit
def test_health_check_destroyed_raises_state_error() -> None:
    """健康检查已销毁的实例应抛 MigStateError。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))
    _run(manager.destroy_instance(instance.instance_id))

    with pytest.raises(MigStateError, match="非法状态转移"):
        _run(manager.health_check(instance.instance_id))


@pytest.mark.unit
def test_health_check_can_toggle_between_healthy_and_degraded() -> None:
    """健康检查可在 Healthy 与 Degraded 之间反复转移。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))

    async def check_healthy(inst_id: str) -> dict[str, Any]:
        return {"status": HEALTHY}

    async def check_degraded(inst_id: str) -> dict[str, Any]:
        return {"status": DEGRADED}

    manager._health_check_fn = check_healthy
    assert _run(manager.health_check(instance.instance_id)).status == HEALTHY
    manager._health_check_fn = check_degraded
    assert _run(manager.health_check(instance.instance_id)).status == DEGRADED
    manager._health_check_fn = check_healthy
    assert _run(manager.health_check(instance.instance_id)).status == HEALTHY


# --------------------------------------------------------------------------- #
# get_capability_report
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_get_capability_report_empty_gpu() -> None:
    """无实例时能力报告应显示全部资源可用。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    report = _run(manager.get_capability_report(0))

    assert isinstance(report, MigCapabilityReport)
    assert report.gpu_id == 0
    assert report.total_memory_gb == 80
    assert report.total_compute_slices == 7
    assert report.used_memory_gb == 0
    assert report.used_compute_slices == 0
    assert report.available_memory_gb == 80
    assert report.available_compute_slices == 7
    assert report.instances == []


@pytest.mark.unit
def test_get_capability_report_with_active_instances() -> None:
    """有活跃实例时能力报告应正确反映已用 / 可用。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    _run(manager.create_instance(0, MIG_PROFILES["3g.20gb"], "t1"))  # 3 切片, 20GB
    _run(manager.create_instance(0, MIG_PROFILES["2g.10gb"], "t2"))  # 2 切片, 10GB

    report = _run(manager.get_capability_report(0))

    assert report.used_memory_gb == 30
    assert report.used_compute_slices == 5
    assert report.available_memory_gb == 50
    assert report.available_compute_slices == 2
    assert len(report.instances) == 2


@pytest.mark.unit
def test_get_capability_report_excludes_destroyed_instances() -> None:
    """已销毁实例不计入能力报告。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    i1 = _run(manager.create_instance(0, MIG_PROFILES["3g.20gb"], "t1"))
    _run(manager.create_instance(0, MIG_PROFILES["2g.10gb"], "t2"))
    _run(manager.destroy_instance(i1.instance_id))

    report = _run(manager.get_capability_report(0))

    assert report.used_memory_gb == 10
    assert report.used_compute_slices == 2
    assert len(report.instances) == 1


# --------------------------------------------------------------------------- #
# 状态机 — 非法转移
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_state_machine_destroyed_is_terminal() -> None:
    """Destroyed 为终态：不能再健康检查或销毁。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    instance = _run(manager.create_instance(0, MIG_PROFILES["1g.5gb"], "t1"))
    _run(manager.destroy_instance(instance.instance_id))

    # 再次销毁 -> 非法
    with pytest.raises(MigStateError):
        _run(manager.destroy_instance(instance.instance_id))
    # 健康检查 -> 非法
    with pytest.raises(MigStateError):
        _run(manager.health_check(instance.instance_id))


@pytest.mark.unit
def test_state_machine_transition_rejects_invalid_target() -> None:
    """_transition 应拒绝不在转移表中的目标状态。"""
    manager = MigManager(gpu_capability_provider=_cap_provider())
    # NotCreated -> Destroyed 是非法的（未创建不能销毁）
    with pytest.raises(MigStateError, match="非法状态转移"):
        manager._transition("fake-id", DESTROYED)


# --------------------------------------------------------------------------- #
# 并发互斥 — 同 GPU 并发创建不超分
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_concurrent_create_on_same_gpu_does_not_oversubscribe_slices() -> None:
    """同 GPU 并发创建 8 个 1 切片实例（总量 7），应仅成功 7 个。"""
    manager = MigManager(gpu_capability_provider=_cap_provider(total_compute_slices=7))
    profile = MIG_PROFILES["1g.5gb"]  # 各 1 切片

    async def run_concurrent() -> list[Any]:
        tasks = [
            manager.create_instance(0, profile, f"tenant-{i}")
            for i in range(8)
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = _run(run_concurrent())
    successes = [r for r in results if isinstance(r, MigInstance)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 7
    assert len(failures) == 1
    assert isinstance(failures[0], MigResourceError)
    # 验证实际切片不超分
    report = _run(manager.get_capability_report(0))
    assert report.used_compute_slices == 7


@pytest.mark.unit
def test_concurrent_create_on_different_gpus_do_not_block_each_other() -> None:
    """不同 GPU 上的并发创建不应互相阻塞（各自独立锁）。"""
    manager = MigManager(gpu_capability_provider=_cap_provider(total_compute_slices=1))

    async def run_concurrent() -> list[Any]:
        tasks = [
            manager.create_instance(i, MIG_PROFILES["1g.5gb"], f"t-{i}")
            for i in range(4)
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = _run(run_concurrent())
    successes = [r for r in results if isinstance(r, MigInstance)]

    assert len(successes) == 4
