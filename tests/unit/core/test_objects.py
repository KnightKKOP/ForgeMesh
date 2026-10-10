"""``forgemesh.core.objects`` 单元测试。

覆盖阶段 0 核心契约的构造与行为：

* ``apiVersion`` 默认值 = ``GROUP_VERSION``；
* ``with_generation_bump`` 生成副本并递增 generation（不改变原对象）；
* ``set_condition`` 对同一 type 就地覆盖、对新 type 追加；
* ``get_condition`` 命中返回条件、未命中返回 ``None``。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from forgemesh.core.objects import (
    GROUP_VERSION,
    Condition,
    ObjectMeta,
    Resource,
)


def _make_resource(**overrides) -> Resource:
    """构造一个可复用的最小 Resource。"""
    params = {
        "kind": "InferenceService",
        "metadata": ObjectMeta(name="demo", namespace="team-a", uid="uid-1"),
    }
    params.update(overrides)
    return Resource(**params)


def test_group_version_constant() -> None:
    assert GROUP_VERSION == "forgemesh.io/v1alpha1"


def test_object_meta_defaults() -> None:
    meta = ObjectMeta(name="demo")
    assert meta.name == "demo"
    assert meta.namespace is None
    assert meta.uid is None
    assert meta.labels == {}
    assert meta.annotations == {}
    assert meta.resourceVersion is None
    assert meta.generation == 0


def test_construct_resource_with_metadata() -> None:
    resource = _make_resource()
    assert resource.apiVersion == GROUP_VERSION
    assert resource.kind == "InferenceService"
    assert resource.metadata.name == "demo"
    assert resource.metadata.namespace == "team-a"
    assert resource.metadata.uid == "uid-1"
    assert resource.conditions == []


def test_construct_condition() -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    cond = Condition(
        type="Ready",
        status="True",
        reason="Deployed",
        message="all replicas ready",
        lastTransitionTime=now,
    )
    assert cond.type == "Ready"
    assert cond.status == "True"
    assert cond.reason == "Deployed"
    assert cond.lastTransitionTime == now


def test_condition_status_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        Condition(type="Ready", status="Maybe")  # type: ignore[arg-type]


def test_with_generation_bump_increments_without_mutating_original() -> None:
    resource = _make_resource()
    assert resource.metadata.generation == 0

    bumped = resource.with_generation_bump()

    assert bumped.metadata.generation == 1
    # 原对象保持不变（不可变派生语义）
    assert resource.metadata.generation == 0
    # 是副本而非同一对象
    assert bumped is not resource
    assert bumped.metadata is not resource.metadata


def test_with_generation_bump_is_cumulative() -> None:
    resource = _make_resource()
    resource = resource.with_generation_bump().with_generation_bump()
    assert resource.metadata.generation == 2


def test_set_condition_overwrites_same_type() -> None:
    resource = _make_resource()
    first = Condition(type="Ready", status="False", reason="Pending")
    second = Condition(type="Ready", status="True", reason="Deployed")

    resource.set_condition(first)
    assert len(resource.conditions) == 1

    resource.set_condition(second)

    # 同一 type 就地覆盖，不新增条目
    assert len(resource.conditions) == 1
    assert resource.get_condition("Ready") is second
    assert resource.get_condition("Ready").status == "True"


def test_set_condition_appends_new_types() -> None:
    resource = _make_resource()
    resource.set_condition(Condition(type="Ready", status="True"))
    resource.set_condition(Condition(type="Scheduled", status="False"))

    assert len(resource.conditions) == 2
    assert [c.type for c in resource.conditions] == ["Ready", "Scheduled"]


def test_set_condition_returns_self_for_chaining() -> None:
    resource = _make_resource()
    returned = resource.set_condition(Condition(type="Ready", status="True"))
    assert returned is resource


def test_get_condition_missing_returns_none() -> None:
    resource = _make_resource()
    assert resource.get_condition("Ready") is None
