"""领域事件信封与资源变更事件。

信封字段与 ``api/schemas/event.schema.json``（DomainEvent）一致。资源注册表在写入资源的
同一个事务中，经 :class:`~forgemesh.controlplane.outbox.Outbox` 写入一条资源变更事件
（ADR-008，architecture §12.3 / §13.2）：Outbox 事件的 ``payload`` 就是信封，``event_id``
与信封的 ``eventId`` 相同。

资源变更事件
------------
``eventType`` 为 ``<kind>.<change>``：kind 按单词拆成小写并以点连接
（``InferenceService`` → ``inference.service``），change 见 :class:`ResourceEventChange`。

``data`` 含 ``kind`` / ``namespace`` / ``name`` / ``change``、``phase``（生命周期阶段，
资源没有 phase 时为 ``null``）与 ``resource``（资源表示，与 API 返回的相同）。``phase``、
``resource`` 与 ``resourceVersion`` 描述写入之后的资源；``deleted`` 事件描述资源删除前
最后的状态，因此它的 ``resourceVersion`` 与删除前最后一次写入的事件相同。
``schemaVersion`` 为 ``v1``，``producer`` 为 ``control-plane`` 与控制平面版本。

事件至少投递一次：消费方按 ``eventId`` 去重；投递顺序只是尽力按写入顺序，同一资源
（``resourceUid``）的事件以 ``resourceVersion`` 判断新旧。
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from .outbox import OutboxEvent
from .version import CONTROL_PLANE_VERSION

#: 资源变更事件 ``data`` 的版本（信封的 ``schemaVersion``）。
RESOURCE_EVENT_SCHEMA_VERSION = "v1"

#: 控制平面产生的事件的 ``producer.component``。
PRODUCER_COMPONENT = "control-plane"

#: kind 中的一个单词：连续大写的缩写（如 ``GPU``）或首字母大写的小写串，数字随前一个单词。
_KIND_WORD = re.compile(r"[A-Z]+[0-9]*(?![a-z])|[A-Z]?[a-z0-9]+")


class ResourceEventChange(StrEnum):
    """资源变更事件的变更类型（``eventType`` 的最后一段与 ``data.change``）。"""

    CREATED = "created"
    """创建资源。"""
    UPDATED = "updated"
    """更新资源：``update_spec`` 的期望状态写入（API 的 PUT / PATCH），或 ``put`` 的
    Update-CAS（含协调器移除 finalizer）。"""
    STATUS_UPDATED = "status_updated"
    """status 子资源写入：控制器回写状态条件、phase 与实例地址。"""
    TERMINATING = "terminating"
    """删除请求已受理：资源进入 ``Terminating``，等待受控释放。"""
    DELETED = "deleted"
    """资源已被物理删除。"""


class EventProducer(BaseModel):
    """事件生产者（信封的 ``producer``）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1)
    version: str = Field(min_length=1)


class DomainEvent(BaseModel):
    """领域事件信封，以别名（驼峰）序列化后符合 ``api/schemas/event.schema.json``。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event_id: str = Field(default_factory=lambda: str(uuid4()), alias="eventId", min_length=1)
    event_type: str = Field(alias="eventType", min_length=1)
    schema_version: str = Field(alias="schemaVersion", min_length=1)
    occurred_at: AwareDatetime = Field(alias="occurredAt")
    tenant_id: str = Field(alias="tenantId", min_length=1)
    resource_id: str = Field(alias="resourceId", min_length=1)
    resource_uid: str | None = Field(default=None, alias="resourceUid")
    resource_version: int | None = Field(default=None, alias="resourceVersion", ge=0)
    correlation_id: str = Field(alias="correlationId", min_length=1)
    producer: EventProducer
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    def to_payload(self) -> dict[str, Any]:
        """JSON 载荷：驼峰字段名，省略为空的可选字段（Schema 不允许它们为 ``null``）。"""
        payload = self.model_dump(mode="json", by_alias=True)
        for key in ("resourceUid", "resourceVersion"):
            if payload[key] is None:
                del payload[key]
        return payload


def event_type_prefix(kind: str) -> str:
    """kind 对应的事件类型前缀：按单词拆成小写并以点连接。

    ``InferenceService`` → ``inference.service``，``GPUNode`` → ``gpu.node``；kind 中没有
    字母和数字时为 ``resource``。
    """
    return ".".join(word.lower() for word in _KIND_WORD.findall(kind)) or "resource"


def resource_event(
    *,
    tenant_id: str,
    kind: str,
    namespace: str | None,
    name: str,
    change: ResourceEventChange,
    resource: dict[str, Any],
    version: int,
    phase: str | None,
    uid: str | None = None,
    correlation_id: str | None = None,
    occurred_at: float | None = None,
) -> OutboxEvent:
    """构造一条资源变更事件：``payload`` 为 :class:`DomainEvent` 信封的 Outbox 事件。

    Args:
        tenant_id: 资源所属租户。
        kind: 资源类型。
        namespace: 命名空间（可为 ``None``）。
        name: 资源名（信封的 ``resourceId``）。
        change: 变更类型。
        resource: 事件描述的资源表示。
        version: 事件描述的资源版本。
        phase: 事件描述的生命周期阶段。
        uid: 资源 uid；为空时信封不含 ``resourceUid``。
        correlation_id: 关联 ID；为空时生成一个新的。
        occurred_at: 发生时间戳（秒），默认当前时间。
    """
    created_at = time.time() if occurred_at is None else occurred_at
    event_type = f"{event_type_prefix(kind)}.{change.value}"
    envelope = DomainEvent(
        event_type=event_type,
        schema_version=RESOURCE_EVENT_SCHEMA_VERSION,
        occurred_at=datetime.fromtimestamp(created_at, UTC),
        tenant_id=tenant_id,
        resource_id=name,
        resource_uid=uid or None,
        resource_version=version,
        correlation_id=correlation_id or uuid4().hex,
        producer=EventProducer(component=PRODUCER_COMPONENT, version=CONTROL_PLANE_VERSION),
        data={
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "change": change.value,
            "phase": phase,
            "resource": resource,
        },
    )
    return OutboxEvent(
        event_id=envelope.event_id,
        aggregate_type=kind,
        aggregate_id=f"{tenant_id}/{namespace or ''}/{name}",
        event_type=event_type,
        payload=envelope.to_payload(),
        created_at=created_at,
    )


__all__ = [
    "PRODUCER_COMPONENT",
    "RESOURCE_EVENT_SCHEMA_VERSION",
    "DomainEvent",
    "EventProducer",
    "ResourceEventChange",
    "event_type_prefix",
    "resource_event",
]
