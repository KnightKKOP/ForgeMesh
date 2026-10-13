"""Finalizer 受控删除闭环契约测试（任务 61 / WP-C1，v1.1 §9.2/§9.3 阶段 C）。

按实现真实 API（``SQLiteRegistry.request_delete`` +
``ReconciliationController._reconcile_terminating``）验证 v1.0.1 交付的
受控删除闭环在正常 / 失败重入 / 未知资源 / 无 finalizer 场景下的一致性：

* 正常闭环：``request_delete`` → ``Terminating``（自动追加默认 finalizer）
  → ReleaseProvider 成功 → finalizer 清空 → 关联 Allocation 释放 →
  物理删除（``get`` 报 NotFound）；
* Release 失败重入：失败后保持 ``Terminating``（资源不误删），再次驱动
  闭环可完成（幂等重入）；
* 未知资源 ``request_delete`` → :class:`NotFoundError`；
* 无 finalizer 且无外部副作用的资源走直接物理删除路径（向后兼容）；
* 已 ``Terminating`` 的重复删除请求幂等（版本不重复递增）。

关联 Allocation 以归属资源 uid 记录：``allocations.resource_id = metadata.uid``
（``reconciler/core.py`` 步骤 3 按 uid、限本租户释放）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from forgemesh.controlplane.reconciler import ReconciliationController
from forgemesh.controlplane.registry import DEFAULT_RELEASE_FINALIZER, SQLiteRegistry
from forgemesh.core.errors import NotFoundError
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.reconciler import ReconcileContext
from forgemesh.policy.api import PolicyDecision
from forgemesh.resource.allocation import Allocation
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

KIND = "InferenceService"
TENANT = "tenant-1"
NAMESPACE = "team-a"


def _run(coro):
    """同步驱动协程（项目测试不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _make_inference_service(
    name: str = "svc-1",
    phase: InferencePhase = InferencePhase.READY,
    finalizers: list[str] | None = None,
) -> InferenceService:
    """构造带强类型 spec/status 的 InferenceService（phase 由 status 携带）。"""
    return InferenceService(
        metadata=ObjectMeta(
            name=name,
            namespace=NAMESPACE,
            uid=f"uid-{name}",
            tenant_id=TENANT,
            generation=1,
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=phase),
        finalizers=finalizers or [],
    )


def _stored_phase(reg: SQLiteRegistry, name: str = "svc-1") -> str | None:
    """直读 DB 列取 phase（与既有单测一致的证据口径）。"""
    row = reg._conn.execute(
        "SELECT phase FROM resources WHERE name=?", (name,)
    ).fetchone()
    return row["phase"] if row is not None else None


def _stored_version(reg: SQLiteRegistry, name: str = "svc-1") -> int | None:
    """直读 DB 列取 resource_version。"""
    row = reg._conn.execute(
        "SELECT resource_version FROM resources WHERE name=?", (name,)
    ).fetchone()
    return row["resource_version"] if row is not None else None


def _stored_finalizers(reg: SQLiteRegistry, name: str = "svc-1") -> list[str]:
    """直读 DB 列取 finalizers。"""
    row = reg._conn.execute(
        "SELECT finalizers FROM resources WHERE name=?", (name,)
    ).fetchone()
    return json.loads(row["finalizers"]) if row is not None else []


class StubReleaseProvider:
    """Stub ReleaseProvider：记录调用，可配置抛异常模拟释放失败。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.released: list[str] = []

    async def release(self, resource: Resource) -> None:
        self.released.append(resource.metadata.name)
        if self.fail:
            raise RuntimeError("release failed: gpu driver error")


class _AllowPolicy:
    """固定 allow 的 PolicyEngine 测试替身（Terminating 子流程不经过 Policy）。"""

    async def check(self, ctx: Any, action: Any) -> PolicyDecision:
        return PolicyDecision(decision="allow", reason="contract-test")


def _make_controller(
    registry: SQLiteRegistry,
    release_provider: Any | None = None,
) -> ReconciliationController:
    return ReconciliationController(
        registry=registry,
        policy_engine=_AllowPolicy(),
        release_provider=release_provider,
    )


def _terminating_context(
    registry: SQLiteRegistry,
    dedup_key: str = "term-1",
) -> ReconcileContext:
    """构造 Terminating 资源的协调上下文（resource 以注册表读回为准注入）。"""
    resource = _run(registry.get(KIND, NAMESPACE, "svc-1", TENANT))
    return ReconcileContext(
        resource=resource,
        observed={"phase": "Terminating"},
        correlation_id="corr-term",
        tenant_id=TENANT,
        dedup_key=dedup_key,
    )


# ---------------------------------------------------------------------- #
# 正常闭环：request_delete → Terminating → Release 成功 → 物理删除
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_happy_path_full_closure_with_allocation() -> None:
    """完整删除闭环：Ready → Terminating → 释放 → finalizer 清空 → Allocation
    Released → 物理删除（get 报 NotFound）。"""
    reg = SQLiteRegistry()
    try:
        _run(reg.put(_make_inference_service(phase=InferencePhase.READY)))
        # 建立归属该资源的 Allocation（resource_id 为资源 uid）
        _run(
            reg.put_allocation(
                Allocation(
                    resource_id="uid-svc-1",
                    tenant_id=TENANT,
                    idempotency_key="alloc-key-1",
                )
            )
        )

        # 受理删除：Ready 有外部副作用 → 自动追加默认 finalizer → Terminating
        result = _run(reg.request_delete(KIND, NAMESPACE, "svc-1", TENANT))
        assert result.deleted is False
        assert result.finalizers == [DEFAULT_RELEASE_FINALIZER]
        assert _stored_phase(reg) == "Terminating"
        # 资源仍在（等待协调器受控释放，不可直接消失）
        assert _run(reg.get(KIND, NAMESPACE, "svc-1", TENANT)) is not None

        stub = StubReleaseProvider()
        controller = _make_controller(reg, release_provider=stub)
        outcome = _run(controller.reconcile(_terminating_context(reg)))

        # ReleaseProvider 被调用且仅一次
        assert stub.released == ["svc-1"]
        assert outcome.done is True
        assert outcome.requeue_after is None

        # 物理删除：get 报 NotFound（而非空对象）
        with pytest.raises(NotFoundError):
            _run(reg.get(KIND, NAMESPACE, "svc-1", TENANT))
        assert _stored_phase(reg) is None

        # 关联 Allocation 已释放（Active → Released，保留历史记录）
        allocs = _run(reg.list_allocations(TENANT))
        assert len(allocs) == 1
        assert allocs[0]["status"] == "Released"
        assert allocs[0]["released_at"] is not None
        assert _run(reg.get_active_allocation("uid-svc-1", TENANT)) is None
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# Release 失败重入：失败保持 Terminating，再次驱动闭环可完成
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_release_failure_keeps_terminating_then_retry_completes() -> None:
    """释放失败 → 保持 Terminating 不误删；重试（换成功 Provider）后闭环完成。"""
    reg = SQLiteRegistry()
    try:
        _run(reg.put(_make_inference_service(phase=InferencePhase.READY)))
        _run(reg.request_delete(KIND, NAMESPACE, "svc-1", TENANT))
        version_at_terminating = _stored_version(reg)

        # 第一轮：ReleaseProvider 失败 → 保持 Terminating + requeue 重试
        failing = StubReleaseProvider(fail=True)
        controller = _make_controller(reg, release_provider=failing)
        first = _run(controller.reconcile(_terminating_context(reg, dedup_key="term-retry")))

        assert first.done is False
        assert first.requeue_after == 10.0
        conditions = first.status_patch["conditions"]
        assert any(c["reason"] == "ReleaseFailed" for c in conditions)
        # 资源仍在、仍 Terminating、finalizer 未被清空（GPU 不泄漏也不误删）
        assert _stored_phase(reg) == "Terminating"
        assert _stored_finalizers(reg) == [DEFAULT_RELEASE_FINALIZER]
        assert _stored_version(reg) == version_at_terminating
        # 失败路径不登记 dedup_key：同 key 下一轮仍会重试
        assert "term-retry" not in controller._processed_keys

        # 第二轮：释放成功 → 闭环完成（幂等重入）
        recovered = StubReleaseProvider()
        retry_controller = _make_controller(reg, release_provider=recovered)
        second = _run(
            retry_controller.reconcile(_terminating_context(reg, dedup_key="term-retry"))
        )

        assert recovered.released == ["svc-1"]
        assert second.done is True
        with pytest.raises(NotFoundError):
            _run(reg.get(KIND, NAMESPACE, "svc-1", TENANT))
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# 未知资源 request_delete
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_request_delete_unknown_resource_not_found() -> None:
    """删除不存在的资源 → NotFoundError（按实现语义断言）。"""
    reg = SQLiteRegistry()
    try:
        with pytest.raises(NotFoundError):
            _run(reg.request_delete(KIND, NAMESPACE, "no-such-svc", TENANT))
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# 无 finalizer 资源：直接物理删除路径（向后兼容）
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_request_delete_without_finalizers_physical_delete() -> None:
    """无副作用态（Pending）且无 finalizer → 不进 Terminating，直接物理删除。"""
    reg = SQLiteRegistry()
    try:
        _run(reg.put(_make_inference_service(phase=InferencePhase.PENDING)))

        result = _run(reg.request_delete(KIND, NAMESPACE, "svc-1", TENANT))

        assert result.deleted is True
        assert result.finalizers == []
        with pytest.raises(NotFoundError):
            _run(reg.get(KIND, NAMESPACE, "svc-1", TENANT))
    finally:
        _run(reg.close())


@pytest.mark.contract
def test_request_delete_legacy_base_resource_physical_delete() -> None:
    """基类 Resource（无 phase payload）无外部副作用 → 直接物理删除。"""
    reg = SQLiteRegistry()
    try:
        legacy = Resource(
            kind=KIND,
            metadata=ObjectMeta(name="legacy", namespace=NAMESPACE, tenant_id=TENANT),
        )
        _run(reg.put(legacy))

        result = _run(reg.request_delete(KIND, NAMESPACE, "legacy", TENANT))

        assert result.deleted is True
        with pytest.raises(NotFoundError):
            _run(reg.get(KIND, NAMESPACE, "legacy", TENANT))
    finally:
        _run(reg.close())


# ---------------------------------------------------------------------- #
# 已 Terminating 的重复删除请求幂等
# ---------------------------------------------------------------------- #


@pytest.mark.contract
def test_request_delete_idempotent_while_terminating() -> None:
    """重复删除请求幂等受理：finalizers 不重复追加、版本不重复递增。"""
    reg = SQLiteRegistry()
    try:
        _run(reg.put(_make_inference_service(phase=InferencePhase.READY)))
        first = _run(reg.request_delete(KIND, NAMESPACE, "svc-1", TENANT))
        assert first.deleted is False
        version_after_first = _stored_version(reg)

        second = _run(reg.request_delete(KIND, NAMESPACE, "svc-1", TENANT))

        assert second.deleted is False
        assert second.finalizers == first.finalizers == [DEFAULT_RELEASE_FINALIZER]
        # 幂等：版本不再递增、finalizers 不重复追加
        assert _stored_version(reg) == version_after_first
        assert _stored_finalizers(reg) == [DEFAULT_RELEASE_FINALIZER]
    finally:
        _run(reg.close())
