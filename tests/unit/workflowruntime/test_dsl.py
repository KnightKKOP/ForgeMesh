"""DSL 解析与类型校验单元测试（对齐 development-plan 5.4）。

覆盖：
* 正常解析；
* 空步骤列表；
* 重复 step id；
* 无效 needs 引用；
* DAG 有环；
* parse_retry_policy。
"""

from __future__ import annotations

import pytest
from forgemesh.workflow.runtime import RetryPolicy, Step, WorkflowDefinition
from forgemesh.workflowruntime.dsl import (
    DSLParseError,
    parse_retry_policy,
    parse_workflow_definition,
    validate_dag,
)


def _make_raw(
    steps: list[dict] | None = None,
    name: str = "test-flow",
) -> dict:
    """构造原始工作流定义 dict。"""
    if steps is None:
        steps = [
            {"id": "a", "uses": "action.a"},
            {"id": "b", "uses": "action.b", "needs": ["a"]},
        ]
    return {
        "name": name,
        "timeout_seconds": 300,
        "steps": steps,
    }


# --------------------------------------------------------------------------- #
# 正常解析
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_parse_workflow_definition_success() -> None:
    """正常解析工作流定义。"""
    raw = _make_raw()
    defn = parse_workflow_definition(raw)

    assert isinstance(defn, WorkflowDefinition)
    assert defn.name == "test-flow"
    assert defn.timeout_seconds == 300
    assert len(defn.steps) == 2
    assert defn.steps[0].id == "a"
    assert defn.steps[1].id == "b"
    assert defn.steps[1].needs == ["a"]


@pytest.mark.unit
def test_parse_workflow_definition_with_alias() -> None:
    """使用 with alias 解析步骤参数。"""
    raw = _make_raw(
        steps=[
            {"id": "s1", "uses": "action.x", "with": {"key": "value"}},
        ]
    )
    defn = parse_workflow_definition(raw)
    assert defn.steps[0].with_ == {"key": "value"}


@pytest.mark.unit
def test_parse_workflow_definition_single_step() -> None:
    """单步骤（无依赖）正常解析。"""
    raw = _make_raw(steps=[{"id": "only", "uses": "action.solo"}])
    defn = parse_workflow_definition(raw)
    assert len(defn.steps) == 1
    assert defn.steps[0].id == "only"


# --------------------------------------------------------------------------- #
# 空步骤
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_parse_empty_steps() -> None:
    """空步骤列表抛 DSLParseError。"""
    raw = _make_raw(steps=[])
    with pytest.raises(DSLParseError, match="at least one step"):
        parse_workflow_definition(raw)


# --------------------------------------------------------------------------- #
# 重复 id
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_parse_duplicate_step_id() -> None:
    """重复 step id 抛 DSLParseError。"""
    raw = _make_raw(
        steps=[
            {"id": "dup", "uses": "action.a"},
            {"id": "dup", "uses": "action.b"},
        ]
    )
    with pytest.raises(DSLParseError, match="duplicate step id"):
        parse_workflow_definition(raw)


# --------------------------------------------------------------------------- #
# 无效 needs
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_parse_unknown_dependency() -> None:
    """needs 引用不存在的步骤抛 DSLParseError。"""
    raw = _make_raw(
        steps=[
            {"id": "a", "uses": "action.a"},
            {"id": "b", "uses": "action.b", "needs": ["nonexistent"]},
        ]
    )
    with pytest.raises(DSLParseError, match="unknown dependency"):
        parse_workflow_definition(raw)


# --------------------------------------------------------------------------- #
# DAG 有环
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_parse_cycle_detected() -> None:
    """依赖图有环抛 DSLParseError。"""
    raw = _make_raw(
        steps=[
            {"id": "a", "uses": "action.a", "needs": ["c"]},
            {"id": "b", "uses": "action.b", "needs": ["a"]},
            {"id": "c", "uses": "action.c", "needs": ["b"]},
        ]
    )
    with pytest.raises(DSLParseError, match="cycle"):
        parse_workflow_definition(raw)


@pytest.mark.unit
def test_parse_self_cycle() -> None:
    """自环依赖抛 DSLParseError。"""
    raw = _make_raw(
        steps=[
            {"id": "a", "uses": "action.a", "needs": ["a"]},
        ]
    )
    with pytest.raises(DSLParseError, match="cycle"):
        parse_workflow_definition(raw)


# --------------------------------------------------------------------------- #
# validate_dag
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_validate_dag_valid() -> None:
    """合法 DAG 通过校验。"""
    steps = [
        Step(id="a", uses="x"),
        Step(id="b", uses="x", needs=["a"]),
        Step(id="c", uses="x", needs=["a", "b"]),
    ]
    validate_dag(steps)  # 不抛异常即通过


@pytest.mark.unit
def test_validate_dag_cycle() -> None:
    """有环 DAG 抛 DSLParseError。"""
    steps = [
        Step(id="a", uses="x", needs=["b"]),
        Step(id="b", uses="x", needs=["a"]),
    ]
    with pytest.raises(DSLParseError, match="cycle"):
        validate_dag(steps)


# --------------------------------------------------------------------------- #
# parse_retry_policy
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_parse_retry_policy_success() -> None:
    """正常解析 RetryPolicy。"""
    raw = {"max_attempts": 5, "backoffSeconds": 1.0, "max_backoff_seconds": 30.0}
    policy = parse_retry_policy(raw)

    assert isinstance(policy, RetryPolicy)
    assert policy.max_attempts == 5
    assert policy.initial_backoff_seconds == 1.0
    assert policy.max_backoff_seconds == 30.0


@pytest.mark.unit
def test_parse_retry_policy_default() -> None:
    """空 dict 使用默认值。"""
    policy = parse_retry_policy({})
    assert policy.max_attempts == 3
    assert policy.initial_backoff_seconds == 2.0
    assert policy.max_backoff_seconds == 60.0


@pytest.mark.unit
def test_parse_retry_policy_invalid() -> None:
    """非法值抛 DSLParseError。"""
    raw = {"max_attempts": 0}  # ge=1 约束
    with pytest.raises(DSLParseError, match="retry policy"):
        parse_retry_policy(raw)


# --------------------------------------------------------------------------- #
# 类型校验失败
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_parse_invalid_type() -> None:
    """字段类型错误抛 DSLParseError。"""
    raw = {"name": 123, "steps": [{"id": "a", "uses": "x"}]}
    with pytest.raises(DSLParseError, match="parse workflow definition"):
        parse_workflow_definition(raw)
