"""``forgemesh.core.scheduler`` 契约单元测试。

覆盖：调度请求必须携带被服务资源的身份（含 uid）与幂等键，未知字段一律拒绝；
``resource_kind`` 作为只读属性保留；分配引用记录占用的设备。
"""

from __future__ import annotations

import pytest
from forgemesh.core.scheduler import AllocatedDevice, AllocationRef, ResourceRef, ScheduleRequest
from pydantic import ValidationError

REF = ResourceRef(kind="InferenceService", namespace="team-a", name="llama", uid="uid-1")


def test_schedule_request_carries_resource_identity_and_idempotency_key() -> None:
    req = ScheduleRequest(tenant_id="acme", resource_ref=REF, idempotency_key="uid-1:1")

    assert req.resource_ref == REF
    assert req.idempotency_key == "uid-1:1"
    assert req.requirements == {}
    assert req.resource_kind == "InferenceService"


@pytest.mark.parametrize("missing", ["resource_ref", "idempotency_key"])
def test_schedule_request_requires_identity_and_key(missing: str) -> None:
    fields = {"tenant_id": "acme", "resource_ref": REF, "idempotency_key": "uid-1:1"}
    del fields[missing]

    with pytest.raises(ValidationError):
        ScheduleRequest(**fields)


def test_schedule_request_rejects_the_removed_resource_kind_field() -> None:
    with pytest.raises(ValidationError):
        ScheduleRequest(
            tenant_id="acme",
            resource_kind="InferenceService",
            resource_ref=REF,
            idempotency_key="uid-1:1",
        )


def test_uid_and_idempotency_key_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        ResourceRef(kind="InferenceService", name="llama", uid="")
    with pytest.raises(ValidationError):
        ScheduleRequest(tenant_id="acme", resource_ref=REF, idempotency_key="")


def test_resource_ref_namespace_is_optional_and_unknown_fields_are_rejected() -> None:
    assert ResourceRef(kind="InferenceService", name="llama", uid="u").namespace is None
    with pytest.raises(ValidationError):
        ResourceRef(kind="InferenceService", name="llama", uid="u", tenant_id="acme")


def test_allocation_ref_records_devices() -> None:
    ref = AllocationRef(
        allocation_id="a-1",
        resource_id="uid-1",
        devices=[AllocatedDevice(node_id="node-a", device_id="gpu-0")],
    )

    assert ref.devices == [AllocatedDevice(node_id="node-a", device_id="gpu-0")]
    assert AllocationRef(allocation_id="a-2", resource_id="uid-2").devices == []
