"""``InferenceService`` 强类型模型单元测试。

覆盖（对齐 development-plan 5.1 / 6.1）：

* 完整构造与默认值（``phase=Pending`` / ``observedGeneration=0`` / ``conditions=[]``）；
* ``AcceleratorSpec.count >= 1`` 与 ``minMemoryGiB >= 1`` 的边界校验；
* ``Availability.replicas >= 0`` 校验；
* ``extra="forbid"`` 拒绝未知字段。
"""

from __future__ import annotations

import pytest
from forgemesh.core.objects import Condition, ObjectMeta
from forgemesh.resource.inference import (
    AcceleratorSpec,
    Availability,
    Endpoint,
    EndpointProtocol,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _make_spec(**overrides: object) -> InferenceServiceSpec:
    """构造一份合法 spec，``overrides`` 可覆盖顶层字段。"""
    fields = {
        "model": ModelRef(reference="models/llama"),
        "runtime": RuntimeRef(provider="vllm", version="0.6.3"),
        "resources": ResourcesSpec(
            accelerator=AcceleratorSpec(type="nvidia-gpu", count=2, minMemoryGiB=80)
        ),
        "availability": Availability(replicas=1),
    }
    fields.update(overrides)
    return InferenceServiceSpec(**fields)


def _make_service(**overrides: object) -> InferenceService:
    """构造一份合法的 InferenceService。"""
    fields = {
        "metadata": ObjectMeta(name="llama-service", namespace="team-a"),
        "spec": _make_spec(),
    }
    fields.update(overrides)
    return InferenceService(**fields)


def test_construct_and_defaults() -> None:
    """完整构造后，kind/apiVersion/status 默认值应符合契约。"""
    service = _make_service()

    assert service.kind == "InferenceService"
    assert service.apiVersion == "forgemesh.io/v1alpha1"
    assert service.metadata.name == "llama-service"
    assert service.spec.resources.accelerator.count == 2
    assert service.spec.resources.accelerator.minMemoryGiB == 80
    assert service.status.phase is InferencePhase.PENDING
    assert service.status.observedGeneration == 0
    assert service.status.conditions == []


def test_status_default_factory_is_isolated() -> None:
    """每个实例的 status/conditions 必须独立（不共享可变默认）。"""
    first = _make_service()
    second = _make_service()

    first.status.conditions.append(
        Condition(type="Ready", status="True", reason="Deployed")
    )

    assert len(first.status.conditions) == 1
    assert second.status.conditions == []


def test_kind_is_fixed() -> None:
    """kind 被固定为 InferenceService 字面量，不接受其他值。"""
    with pytest.raises(ValidationError):
        _make_service(kind="NotInferenceService")


@pytest.mark.parametrize("count", [0, -1, -100])
def test_accelerator_count_must_be_positive(count: int) -> None:
    """count 必须 >= 1，非法取值应被拒绝。"""
    with pytest.raises(ValidationError):
        AcceleratorSpec(type="nvidia-gpu", count=count, minMemoryGiB=80)


@pytest.mark.parametrize("min_memory", [0, -1, -80])
def test_accelerator_min_memory_must_be_positive(min_memory: int) -> None:
    """minMemoryGiB 必须 >= 1，非法取值应被拒绝。"""
    with pytest.raises(ValidationError):
        AcceleratorSpec(type="nvidia-gpu", count=1, minMemoryGiB=min_memory)


def test_accelerator_count_defaults_to_one() -> None:
    """count 省略时默认 1。"""
    accelerator = AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
    assert accelerator.count == 1


@pytest.mark.parametrize("replicas", [-1, -5])
def test_availability_replicas_non_negative(replicas: int) -> None:
    """replicas 必须 >= 0。"""
    with pytest.raises(ValidationError):
        Availability(replicas=replicas)


def test_availability_replicas_zero_allowed() -> None:
    """replicas=0（显式缩容到零）应被允许。"""
    assert Availability(replicas=0).replicas == 0


def test_availability_defaults_to_one() -> None:
    """availability 省略时默认单副本。"""
    spec = _make_spec(availability=Availability())
    assert spec.availability.replicas == 1


def test_extra_field_forbidden() -> None:
    """spec 与模型均拒绝未知字段（extra=forbid）。"""
    with pytest.raises(ValidationError):
        AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=1, unexpected=1)

    with pytest.raises(ValidationError):
        _make_service(surprise=True)


def test_status_can_be_provided_explicitly() -> None:
    """可显式提供 status；phase 接受字符串字面量。"""
    service = _make_service(
        status=InferenceServiceStatus(phase="Ready", observedGeneration=3)
    )
    assert service.status.phase is InferencePhase.READY
    assert service.status.observedGeneration == 3


def test_status_endpoints_default_to_empty_and_ready() -> None:
    """status.endpoints 默认为空；未声明 ready 的实例视为就绪。"""
    endpoint = Endpoint(url="http://10.0.0.1:8000", protocol="openai-chat")

    assert _make_service().status.endpoints == []
    assert endpoint.ready is True
    assert endpoint.protocol is EndpointProtocol.OPENAI_CHAT
    assert endpoint.model_dump(mode="json") == {
        "url": "http://10.0.0.1:8000",
        "ready": True,
        "instanceId": None,
        "protocol": "openai-chat",
    }


@pytest.mark.parametrize(
    "url",
    [
        "10.0.0.1:8000",
        "ftp://10.0.0.1",
        "http://",
        "http://user:secret@10.0.0.1:8000",
        "http://10.0.0.1:8000/?debug=1",
        "http://10.0.0.1:8000/#frag",
        "http://10.0.0.1:notaport",
    ],
)
def test_endpoint_url_must_be_a_plain_http_base_url(url: str) -> None:
    with pytest.raises(ValidationError):
        Endpoint(url=url, protocol="openai-chat")


def test_endpoint_protocol_must_be_known() -> None:
    with pytest.raises(ValidationError):
        Endpoint(url="http://10.0.0.1:8000", protocol="grpc")


def test_inference_phase_has_eleven_states() -> None:
    """InferencePhase 应覆盖 11 个生命周期阶段。"""
    assert len(InferencePhase) == 11
    assert {phase.value for phase in InferencePhase} == {
        "Pending",
        "Scheduling",
        "Preparing",
        "Deploying",
        "Verifying",
        "Ready",
        "Degraded",
        "Updating",
        "Terminating",
        "Failed",
        "Unknown",
    }
