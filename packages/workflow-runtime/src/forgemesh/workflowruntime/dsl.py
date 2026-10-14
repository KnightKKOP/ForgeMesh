"""WorkflowDefinition DSL 解析与类型校验。

从 dict 解析工作流定义，校验步骤 DAG 无环、id 唯一、needs 引用合法。
对齐 development-plan 5.4。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from forgemesh.workflow.runtime import RetryPolicy, Step, WorkflowDefinition
from pydantic import ValidationError


class DSLParseError(Exception):
    """DSL 解析或校验错误。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


def validate_dag(steps: list[Step]) -> None:
    """校验步骤列表构成 DAG（无环），使用 Kahn 拓扑排序。

    Args:
        steps: 步骤列表。

    Raises:
        DSLParseError: 当依赖图中存在环时。
    """
    step_ids = {s.id for s in steps}

    # 构建邻接表与入度表。
    adjacency: dict[str, list[str]] = {s.id: [] for s in steps}
    in_degree: dict[str, int] = {s.id: 0 for s in steps}

    for step in steps:
        for dep in step.needs:
            if dep in step_ids:
                adjacency[dep].append(step.id)
                in_degree[step.id] += 1

    # Kahn 拓扑排序。
    queue: deque[str] = deque(sid for sid, deg in in_degree.items() if deg == 0)
    processed = 0
    while queue:
        node = queue.popleft()
        processed += 1
        for successor in adjacency[node]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if processed < len(steps):
        # 存在环：找出参与环的节点（入度仍 > 0 的节点）。
        cycle_nodes = [sid for sid, deg in in_degree.items() if deg > 0]
        raise DSLParseError(
            "dependency cycle detected among steps",
            details={"cycle_nodes": sorted(cycle_nodes)},
        )


def parse_workflow_definition(raw: dict[str, Any]) -> WorkflowDefinition:
    """从 dict 解析工作流定义并校验 DAG 约束。

    校验项：
    * steps 非空；
    * step.id 唯一；
    * step.needs 引用存在（指向已知 step.id）；
    * 依赖图无环（DAG）。

    Args:
        raw: 原始 dict（通常来自 YAML/JSON 解析）。

    Returns:
        解析后的 :class:`WorkflowDefinition`。

    Raises:
        DSLParseError: 解析或校验失败。
    """
    # 1. pydantic 类型校验。
    try:
        defn = WorkflowDefinition.model_validate(raw)
    except ValidationError as exc:
        raise DSLParseError(
            "failed to parse workflow definition",
            details={"errors": exc.errors()},
        ) from exc

    # 2. steps 非空。
    if not defn.steps:
        raise DSLParseError("workflow definition must have at least one step")

    # 3. step.id 唯一。
    step_ids: list[str] = [s.id for s in defn.steps]
    seen: set[str] = set()
    duplicates: list[str] = []
    for sid in step_ids:
        if sid in seen:
            duplicates.append(sid)
        seen.add(sid)
    if duplicates:
        raise DSLParseError(
            "duplicate step id(s) detected",
            details={"duplicates": sorted(set(duplicates))},
        )

    # 4. step.needs 引用存在。
    id_set = set(step_ids)
    for step in defn.steps:
        for dep in step.needs:
            if dep not in id_set:
                raise DSLParseError(
                    f"step '{step.id}' references unknown dependency '{dep}'",
                    details={"step_id": step.id, "unknown_dependency": dep},
                )

    # 5. DAG 无环。
    validate_dag(defn.steps)

    return defn


def parse_retry_policy(raw: dict[str, Any]) -> RetryPolicy:
    """从 dict 解析 RetryPolicy。

    Args:
        raw: 原始 dict。

    Returns:
        解析后的 :class:`RetryPolicy`。

    Raises:
        DSLParseError: 解析失败。
    """
    try:
        return RetryPolicy.model_validate(raw)
    except ValidationError as exc:
        raise DSLParseError(
            "failed to parse retry policy",
            details={"errors": exc.errors()},
        ) from exc


__all__ = [
    "DSLParseError",
    "parse_workflow_definition",
    "validate_dag",
    "parse_retry_policy",
]
