"""``forgemesh.workflow.runtime`` 契约单元测试（向后兼容的新增项在此登记）。

覆盖：:class:`RunInfo` 快照模型；``get_run`` / ``find_run`` / ``find_runs`` 是带默认实现的
非抽象方法，只实现四个抽象方法的既有运行时（如 Temporal 插件）不受影响。
"""

from __future__ import annotations

import asyncio
from typing import Any

import forgemesh.workflow as workflow_api
import pytest
from forgemesh.workflow.runtime import RunInfo, RunRef, WorkflowDefinition, WorkflowRuntime
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from pydantic import ValidationError


class _StatusOnlyRuntime(WorkflowRuntime):
    """只实现抽象方法的运行时。"""

    async def start(
        self, defn: WorkflowDefinition, inputs: dict[str, Any], idempotency_key: str
    ) -> RunRef:
        return RunRef(run_id="run-1", definition_version=1)

    async def get_status(self, run_id: str) -> WorkflowStatus:
        return WorkflowStatus.RUNNING

    async def cancel(self, run_id: str, cleanup: bool = False) -> None:
        return None

    async def signal(self, run_id: str, name: str, payload: dict[str, Any]) -> None:
        return None


def test_the_abstract_methods_are_unchanged() -> None:
    assert WorkflowRuntime.__abstractmethods__ == {"start", "get_status", "cancel", "signal"}


def test_get_run_defaults_to_the_status_and_the_lookups_are_optional() -> None:
    runtime = _StatusOnlyRuntime()

    info = asyncio.run(runtime.get_run("run-1"))

    assert info == RunInfo(run_id="run-1", status=WorkflowStatus.RUNNING)
    assert (info.steps, info.outputs, info.error) == ({}, {}, None)
    with pytest.raises(NotImplementedError):
        asyncio.run(runtime.find_run("uid-1:1"))
    with pytest.raises(NotImplementedError):
        asyncio.run(runtime.find_runs("uid-1"))


def test_run_info_is_strict_and_frozen() -> None:
    info = RunInfo(
        run_id="run-1",
        status="Failed",
        steps={"launch": "Compensated"},
        outputs={"launch": {"instanceId": "i-1"}},
        error="step 'launch' failed: boom",
    )

    assert info.status is WorkflowStatus.FAILED
    assert info.steps == {"launch": StepStatus.COMPENSATED}
    with pytest.raises(ValidationError):
        RunInfo(run_id="run-1", status="Running", definition="x")
    with pytest.raises(ValidationError):
        RunInfo(run_id="run-1", status="Exploded")
    with pytest.raises(ValidationError):
        info.error = None


@pytest.mark.parametrize("status", list(WorkflowStatus))
def test_terminal_matches_the_state_machine(status: WorkflowStatus) -> None:
    terminal = status in {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMEDOUT,
    }

    assert RunInfo(run_id="run-1", status=status).terminal is terminal


def test_run_info_is_exported_from_the_package() -> None:
    assert workflow_api.RunInfo is RunInfo
    assert "RunInfo" in workflow_api.__all__
