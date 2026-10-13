"""推理服务部署单元测试：启动器契约、部署工作流、删除时的清理与测试替身。

覆盖：

* ``LaunchRequest`` / ``LaunchedInstance`` 的校验，实例地址与 ``status.endpoints`` 同规则；
* 部署工作流的步骤、时限与幂等键（uid + generation），按资源查找部署运行；步骤 handler
  调用启动器，``launch-instance`` 的补偿停止实例；
* 在 ``LocalWorkflowRuntime`` 中执行：成功、验证失败时停止已启动的实例、启动失败、
  启动中途重启后复用同一实例；
* ``DeploymentReleaser``：没有部署时不做事、停止已部署的实例、取消进行中的部署、
  重试补偿时没能停止的实例、停止资源每一次部署运行的实例、停止失败时抛出异常；
* ``InMemoryLauncher`` 按实例幂等键复用实例，只把仍在运行的实例报告为运行中。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

import pytest
from forgemesh.controlplane.deployment import (
    DEPLOYMENT_WORKFLOW,
    LAUNCH_INSTANCE,
    PREPARE_MODEL,
    VERIFY_INSTANCE,
    DeploymentReleaser,
    DeploymentSteps,
    InMemoryLauncher,
    LaunchedInstance,
    LaunchRequest,
    RuntimeLauncher,
    deployment_inputs,
    deployment_run_key,
    deployment_workflow,
    find_deployment_runs,
    launched_instance,
)
from forgemesh.controlplane.deployment.workflow import (
    LAUNCH_TIMEOUT_SECONDS,
    PREPARE_TIMEOUT_SECONDS,
    VERIFY_TIMEOUT_SECONDS,
)
from forgemesh.core.objects import ObjectMeta, Resource
from forgemesh.core.scheduler import AllocatedDevice, ResourceRef
from forgemesh.resource.inference import (
    AcceleratorSpec,
    Endpoint,
    EndpointProtocol,
    InferenceServiceSpec,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)
from forgemesh.workflow.runtime import RetryPolicy, RunInfo, Step
from forgemesh.workflow.states import StepStatus, WorkflowStatus
from forgemesh.workflowruntime.approval import ApprovalGate
from forgemesh.workflowruntime.compensate import CompensationExecutor
from forgemesh.workflowruntime.exec import StepExecutor, WorkflowExecutor
from forgemesh.workflowruntime.runtime import LocalWorkflowRuntime
from forgemesh.workflowruntime.store import WorkflowStore
from pydantic import ValidationError

KEY = deployment_run_key("uid-1", 1)


def _request(key: str = KEY) -> LaunchRequest:
    return LaunchRequest(
        instance_key=key,
        tenant_id="tenant-1",
        resource=ResourceRef(
            kind="InferenceService", namespace="team-a", name="svc-1", uid="uid-1"
        ),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        allocation_id="alloc-1",
        devices=(AllocatedDevice(node_id="node-a", device_id="gpu-0"),),
    )


def _resource(uid: str | None = "uid-1", generation: int = 1) -> Resource:
    return Resource(
        kind="InferenceService",
        metadata=ObjectMeta(
            name="svc-1", namespace="team-a", uid=uid, generation=generation, tenant_id="tenant-1"
        ),
    )


class _ScriptedLauncher(InMemoryLauncher):
    """可让各个调用失败或在中途阻塞的启动器。

    ``block`` / ``fail`` 中的名称：``prepare``、``launch``（创建实例之前）、``launched``
    （创建实例之后、返回之前，模拟结果落盘前进程崩溃）、``verify``。
    """

    def __init__(self) -> None:
        super().__init__()
        self.block: set[str] = set()
        self.fail: set[str] = set()
        self.stop_failures = 0
        self.entered: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.cancelled: list[str] = []

    async def _gate(self, name: str) -> None:
        self.entered[name].set()
        if name in self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append(name)
                raise
        if name in self.fail:
            raise RuntimeError(f"{name} broke")

    async def prepare(self, request: LaunchRequest) -> None:
        await self._gate("prepare")
        await super().prepare(request)

    async def launch(self, request: LaunchRequest) -> LaunchedInstance:
        await self._gate("launch")
        instance = await super().launch(request)
        await self._gate("launched")
        return instance

    async def verify(self, instance: LaunchedInstance) -> None:
        await self._gate("verify")
        await super().verify(instance)

    async def stop(self, instance_id: str) -> None:
        if self.stop_failures:
            self.stop_failures -= 1
            raise RuntimeError("stop broke")
        await super().stop(instance_id)


def _runtime(launcher: RuntimeLauncher, store: WorkflowStore | None = None) -> LocalWorkflowRuntime:
    steps = DeploymentSteps(launcher)
    compensation = CompensationExecutor()
    for step_id, handler in steps.compensations().items():
        compensation.registry.register(step_id, handler)
    retry = RetryPolicy(max_attempts=2, initial_backoff_seconds=0.0, max_backoff_seconds=0.0)
    return LocalWorkflowRuntime(
        store=store if store is not None else WorkflowStore(),
        executor=WorkflowExecutor(StepExecutor(retry)),
        compensation=compensation,
        approval=ApprovalGate(),
        handler=steps.handle,
    )


async def _deploy(runtime: LocalWorkflowRuntime, key: str = KEY) -> str:
    ref = await runtime.start(deployment_workflow(), deployment_inputs(_request(key)), key)
    return ref.run_id


# ---------------------------------------------------------------------- #
# 启动器契约
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_launch_request_round_trips_through_the_workflow_inputs() -> None:
    inputs = deployment_inputs(_request())

    assert json.loads(json.dumps(inputs)) == inputs
    assert LaunchRequest.model_validate(inputs["request"]) == _request()


@pytest.mark.unit
def test_launch_request_is_strict_and_frozen() -> None:
    data = _request().model_dump()
    with pytest.raises(ValidationError):
        LaunchRequest.model_validate({**data, "replicas": 2})
    with pytest.raises(ValidationError):
        LaunchRequest.model_validate({**data, "instance_key": ""})
    with pytest.raises(ValidationError):
        _request().allocation_id = "alloc-2"


@pytest.mark.unit
def test_launched_instance_is_a_ready_endpoint() -> None:
    instance = LaunchedInstance(
        instance_id="i-1", url="http://10.0.0.1:8000", protocol="tgi-generate"
    )

    assert instance.endpoint() == Endpoint(
        url="http://10.0.0.1:8000",
        ready=True,
        instanceId="i-1",
        protocol=EndpointProtocol.TGI_GENERATE,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    ["ftp://10.0.0.1", "http://user:pw@10.0.0.1", "http://10.0.0.1/?q=1", "http://h:99999", "h"],
)
def test_launched_instance_rejects_urls_that_endpoints_reject(url: str) -> None:
    with pytest.raises(ValidationError):
        LaunchedInstance(instance_id="i-1", url=url, protocol="openai-chat")


@pytest.mark.unit
def test_runtime_launcher_requires_launch_verify_stop_and_is_running() -> None:
    """``prepare`` 与 ``aclose`` 可选，默认什么都不做。"""
    assert RuntimeLauncher.__abstractmethods__ == {"launch", "verify", "stop", "is_running"}

    class _Minimal(RuntimeLauncher):
        async def launch(self, request: LaunchRequest) -> LaunchedInstance:
            raise NotImplementedError

        async def verify(self, instance: LaunchedInstance) -> None:
            return None

        async def stop(self, instance_id: str) -> None:
            return None

        async def is_running(self, instance_id: str) -> bool:
            return False

    launcher = _Minimal()
    assert asyncio.run(launcher.prepare(_request())) is None
    assert asyncio.run(launcher.aclose()) is None


# ---------------------------------------------------------------------- #
# 部署工作流
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_deployment_workflow_prepares_launches_and_verifies() -> None:
    defn = deployment_workflow()

    assert defn.name == DEPLOYMENT_WORKFLOW
    assert defn.timeout_seconds is None
    assert [(step.id, step.uses, step.needs, step.timeout) for step in defn.steps] == [
        (PREPARE_MODEL, "deployment.prepare-model", [], PREPARE_TIMEOUT_SECONDS),
        (LAUNCH_INSTANCE, "deployment.launch-instance", [PREPARE_MODEL], LAUNCH_TIMEOUT_SECONDS),
        (VERIFY_INSTANCE, "deployment.verify-instance", [LAUNCH_INSTANCE], VERIFY_TIMEOUT_SECONDS),
    ]
    assert deployment_workflow() is not defn


@pytest.mark.unit
def test_deployment_run_key_is_per_resource_uid_and_generation() -> None:
    assert KEY == "deployment:uid-1:1"
    assert deployment_run_key("uid-1", 2) == "deployment:uid-1:2"
    assert deployment_run_key("uid-2", 1) != KEY


@pytest.mark.unit
def test_find_deployment_runs_returns_the_runs_of_one_resource_in_creation_order() -> None:
    """包括 1.1.0 之前不含 generation 的键；uid 互为前缀的其他资源不算在内。"""
    keys = [
        "deployment:uid-1:2",
        "deployment:uid-1",
        "deployment:uid-10:1",
        "deployment:uid-1:1",
        "deployment:uid-1:x",
        "deployment:uid-1:12",
    ]

    async def scenario() -> dict[int | None, list[tuple[str, int | None]]]:
        runtime = _runtime(InMemoryLauncher())
        try:
            run_ids = {key: await _deploy(runtime, key) for key in keys}
            found = {}
            for generation in (None, 1, 2, 3):
                runs = await find_deployment_runs(runtime, "uid-1", generation)
                assert all(run.run.run_id == run_ids[run.key] for run in runs)
                found[generation] = [(run.key, run.generation) for run in runs]
            return found
        finally:
            await runtime.aclose()

    found = asyncio.run(scenario())

    assert found[None] == [
        ("deployment:uid-1:2", 2),
        ("deployment:uid-1", None),
        ("deployment:uid-1:1", 1),
        ("deployment:uid-1:12", 12),
    ]
    assert found[1] == [("deployment:uid-1:1", 1)]
    assert found[2] == [("deployment:uid-1:2", 2)]
    assert found[3] == []


@pytest.mark.unit
def test_steps_call_the_launcher() -> None:
    launcher = InMemoryLauncher()
    steps = DeploymentSteps(launcher)
    by_id = {step.id: step for step in deployment_workflow().steps}
    inputs = deployment_inputs(_request())

    async def scenario() -> None:
        assert await steps.handle(by_id[PREPARE_MODEL], inputs) == {}
        launched = await steps.handle(by_id[LAUNCH_INSTANCE], inputs)
        assert (
            await steps.handle(by_id[VERIFY_INSTANCE], {**inputs, LAUNCH_INSTANCE: launched}) == {}
        )
        [instance] = launcher.running.values()
        assert launched == instance.model_dump(mode="json")
        with pytest.raises(ValueError, match="not a deployment step"):
            await steps.handle(Step(id="other", uses="test.other"), inputs)

    asyncio.run(scenario())
    assert launcher.prepared == launcher.launches == [_request()]


@pytest.mark.unit
def test_launch_compensation_stops_the_instance() -> None:
    launcher = InMemoryLauncher()
    steps = DeploymentSteps(launcher)
    launch = deployment_workflow().steps[1]

    async def scenario() -> None:
        instance = await launcher.launch(_request())
        [(step_id, compensate)] = steps.compensations().items()
        assert step_id == LAUNCH_INSTANCE
        assert await compensate(launch, instance.model_dump(mode="json")) == {}
        assert launcher.stops == [instance.instance_id]

    asyncio.run(scenario())
    assert launcher.running == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "may_run"),
    [
        (StepStatus.SUCCEEDED, True),
        (StepStatus.COMPENSATING, True),
        (StepStatus.COMPENSATION_FAILED, True),
        (StepStatus.COMPENSATED, False),
        (StepStatus.FAILED, False),
        (StepStatus.CANCELLED, False),
        (StepStatus.SKIPPED, False),
        (StepStatus.PENDING, False),
    ],
)
def test_launched_instance_reports_instances_that_may_still_run(
    status: StepStatus, may_run: bool
) -> None:
    instance = LaunchedInstance(
        instance_id="i-1", url="http://10.0.0.1:8000", protocol="openai-chat"
    )
    run = RunInfo(
        run_id="run-1",
        status=WorkflowStatus.RUNNING,
        steps={LAUNCH_INSTANCE: status},
        outputs={LAUNCH_INSTANCE: instance.model_dump(mode="json")},
    )

    assert launched_instance(run) == (instance if may_run else None)
    assert launched_instance(run.model_copy(update={"outputs": {}})) is None


# ---------------------------------------------------------------------- #
# 在工作流运行时中执行
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_deployment_run_launches_and_verifies_an_instance() -> None:
    launcher = InMemoryLauncher()

    async def scenario() -> RunInfo:
        runtime = _runtime(launcher)
        try:
            run_id = await _deploy(runtime)
            await runtime.wait(run_id)
            assert (await runtime.find_run(KEY)).run_id == run_id
            return await runtime.get_run(run_id)
        finally:
            await runtime.aclose()

    run = asyncio.run(scenario())

    assert run.status is WorkflowStatus.SUCCEEDED
    [instance] = launcher.running.values()
    assert launched_instance(run) == instance
    assert [request.instance_key for request in launcher.launches] == [KEY]


@pytest.mark.unit
def test_failed_verification_stops_the_launched_instance() -> None:
    launcher = _ScriptedLauncher()
    launcher.fail.add("verify")

    async def scenario() -> RunInfo:
        runtime = _runtime(launcher)
        try:
            return await runtime.wait(await _deploy(runtime))
        finally:
            await runtime.aclose()

    run = asyncio.run(scenario())

    assert run.status is WorkflowStatus.FAILED
    assert run.error == "step 'verify-instance' failed: verify broke"
    assert run.steps == {
        PREPARE_MODEL: StepStatus.SUCCEEDED,
        LAUNCH_INSTANCE: StepStatus.COMPENSATED,
        VERIFY_INSTANCE: StepStatus.FAILED,
    }
    assert launcher.running == {}
    assert launcher.stops == [run.outputs[LAUNCH_INSTANCE]["instance_id"]]
    assert launched_instance(run) is None


@pytest.mark.unit
def test_failed_launch_leaves_nothing_to_stop() -> None:
    launcher = _ScriptedLauncher()
    launcher.fail.add("launch")

    async def scenario() -> RunInfo:
        runtime = _runtime(launcher)
        try:
            return await runtime.wait(await _deploy(runtime))
        finally:
            await runtime.aclose()

    run = asyncio.run(scenario())

    assert run.status is WorkflowStatus.FAILED
    assert run.steps[LAUNCH_INSTANCE] is StepStatus.FAILED
    assert run.steps[VERIFY_INSTANCE] is StepStatus.SKIPPED
    assert launcher.stops == []
    assert launcher.running == {}


@pytest.mark.unit
def test_restart_during_launch_reuses_the_running_instance(tmp_path) -> None:
    """实例已创建、结果还没落盘时进程退出：恢复后重新执行启动，启动器按幂等键返回同一实例。"""
    path = str(tmp_path / "workflows.db")
    launcher = _ScriptedLauncher()
    launcher.block.add("launched")

    async def interrupted() -> str:
        store = WorkflowStore(path)
        runtime = _runtime(launcher, store)
        run_id = await _deploy(runtime)
        await launcher.entered["launched"].wait()
        await runtime.aclose()
        await store.close()
        return run_id

    run_id = asyncio.run(interrupted())
    assert launcher.cancelled == ["launched"]
    launcher.block.clear()

    async def resumed() -> RunInfo:
        store = WorkflowStore(path)
        runtime = _runtime(launcher, store)
        try:
            assert await runtime.recover() == [run_id]
            return await runtime.wait(run_id)
        finally:
            await runtime.aclose()
            await store.close()

    run = asyncio.run(resumed())

    assert run.status is WorkflowStatus.SUCCEEDED
    [instance] = launcher.running.values()
    assert launched_instance(run) == instance
    assert [request.instance_key for request in launcher.launches] == [KEY, KEY]


# ---------------------------------------------------------------------- #
# 删除时的清理
# ---------------------------------------------------------------------- #


async def _release(
    launcher: _ScriptedLauncher, resource: Resource, *, until: str | None = None
) -> tuple[RunInfo | None, list[str]]:
    """部署（可先等到某个调用开始）后释放资源，返回运行快照与释放期间停止的实例。"""
    runtime = _runtime(launcher)
    try:
        run_id = await _deploy(runtime)
        if until is None:
            await runtime.wait(run_id)
        else:
            await launcher.entered[until].wait()
        stops_before = len(launcher.stops)
        await DeploymentReleaser(runtime, launcher).release(resource)
        return await runtime.find_run(KEY), launcher.stops[stops_before:]
    finally:
        await runtime.aclose()


@pytest.mark.unit
@pytest.mark.parametrize("resource", [_resource(uid="uid-2"), _resource(uid=None)])
def test_releaser_does_nothing_without_a_deployment_run(resource: Resource) -> None:
    launcher = _ScriptedLauncher()

    async def scenario() -> list[str]:
        runtime = _runtime(launcher)
        try:
            await runtime.wait(await _deploy(runtime))
            await DeploymentReleaser(runtime, launcher).release(resource)
            return launcher.stops
        finally:
            await runtime.aclose()

    assert asyncio.run(scenario()) == []
    assert len(launcher.running) == 1


@pytest.mark.unit
@pytest.mark.parametrize("generation", [1, 2])
def test_releaser_stops_the_instance_of_a_finished_deployment(generation: int) -> None:
    """部署按 uid 查找：generation 变化后删除也能找到并停止实例。"""
    launcher = _ScriptedLauncher()

    run, stops = asyncio.run(_release(launcher, _resource(generation=generation)))

    assert run.status is WorkflowStatus.SUCCEEDED
    assert stops == [run.outputs[LAUNCH_INSTANCE]["instance_id"]]
    assert launcher.running == {}


@pytest.mark.unit
def test_releaser_cancels_a_launch_in_progress() -> None:
    """启动被中断：启动器自己清理，运行以 Cancelled 结束，没有实例需要停止。"""
    launcher = _ScriptedLauncher()
    launcher.block.add("launch")

    run, stops = asyncio.run(_release(launcher, _resource(), until="launch"))

    assert run.status is WorkflowStatus.CANCELLED
    assert run.steps[LAUNCH_INSTANCE] is StepStatus.CANCELLED
    assert launcher.cancelled == ["launch"]
    assert stops == []
    assert launcher.running == {}


@pytest.mark.unit
def test_releaser_cancels_a_verification_in_progress() -> None:
    """实例已启动、正在验证：取消时补偿停止实例，释放器不再重复停止。"""
    launcher = _ScriptedLauncher()
    launcher.block.add("verify")

    run, stops = asyncio.run(_release(launcher, _resource(), until="verify"))

    assert run.status is WorkflowStatus.CANCELLED
    assert run.steps[LAUNCH_INSTANCE] is StepStatus.COMPENSATED
    assert stops == [run.outputs[LAUNCH_INSTANCE]["instance_id"]]
    assert launcher.running == {}


@pytest.mark.unit
def test_releaser_retries_a_stop_that_failed_during_compensation() -> None:
    launcher = _ScriptedLauncher()
    launcher.fail.add("verify")
    launcher.stop_failures = 1

    run, stops = asyncio.run(_release(launcher, _resource()))

    assert run.status is WorkflowStatus.FAILED
    assert run.steps[LAUNCH_INSTANCE] is StepStatus.COMPENSATION_FAILED
    assert stops == [run.outputs[LAUNCH_INSTANCE]["instance_id"]]
    assert launcher.running == {}


@pytest.mark.unit
def test_releaser_stops_the_instances_of_every_deployment_run(caplog) -> None:
    """每一代 spec 与 1.1.0 之前的运行都被处理；只为停止时仍在运行的实例记日志。"""
    launcher = _ScriptedLauncher()

    async def scenario() -> tuple[list[RunInfo], list[str]]:
        runtime = _runtime(launcher)
        try:
            legacy = await runtime.wait(await _deploy(runtime, "deployment:uid-1"))
            await launcher.stop(launched_instance(legacy).instance_id)
            first = await runtime.wait(await _deploy(runtime, KEY))
            launcher.entered["verify"].clear()
            launcher.block.add("verify")
            second_id = await _deploy(runtime, deployment_run_key("uid-1", 2))
            await launcher.entered["verify"].wait()
            stops_before = len(launcher.stops)
            with caplog.at_level(logging.INFO, logger="forgemesh.controlplane.deployment.release"):
                await DeploymentReleaser(runtime, launcher).release(_resource(generation=2))
            return [legacy, first, await runtime.get_run(second_id)], launcher.stops[stops_before:]
        finally:
            await runtime.aclose()

    (legacy, first, second), stops = asyncio.run(scenario())

    assert second.status is WorkflowStatus.CANCELLED
    assert stops == [run.outputs[LAUNCH_INSTANCE]["instance_id"] for run in (legacy, first, second)]
    assert launcher.running == {}
    [stopped] = [record.getMessage() for record in caplog.records]
    assert stopped.startswith(
        f"instance stopped: instance_id={launched_instance(first).instance_id}"
    )


@pytest.mark.unit
def test_releaser_raises_when_the_instance_cannot_be_stopped() -> None:
    launcher = _ScriptedLauncher()
    launcher.stop_failures = 1

    with pytest.raises(RuntimeError, match="stop broke"):
        asyncio.run(_release(launcher, _resource()))
    assert len(launcher.running) == 1


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_in_memory_launcher_reuses_instances_per_key() -> None:
    launcher = InMemoryLauncher(protocol=EndpointProtocol.TGI_GENERATE)

    async def scenario() -> dict[str, Any]:
        first = await launcher.launch(_request())
        again = await launcher.launch(_request())
        other = await launcher.launch(_request(deployment_run_key("uid-2", 1)))
        await launcher.verify(first)
        await launcher.stop(first.instance_id)
        await launcher.stop(first.instance_id)
        with pytest.raises(RuntimeError, match="is not running"):
            await launcher.verify(first)
        relaunched = await launcher.launch(_request())
        return {"first": first, "again": again, "other": other, "relaunched": relaunched}

    instances = asyncio.run(scenario())

    assert instances["again"] == instances["first"]
    assert instances["first"].protocol is EndpointProtocol.TGI_GENERATE
    assert instances["first"].url == f"http://{instances['first'].instance_id}.launcher.invalid"
    assert len({instance.instance_id for instance in instances.values()}) == 3
    assert set(launcher.running) == {
        instances["other"].instance_id,
        instances["relaunched"].instance_id,
    }
    assert launcher.stops == [instances["first"].instance_id] * 2


@pytest.mark.unit
def test_in_memory_launcher_reports_only_running_instances() -> None:
    launcher = InMemoryLauncher()

    async def scenario() -> list[bool]:
        instance = await launcher.launch(_request())
        running = await launcher.is_running(instance.instance_id)
        await launcher.stop(instance.instance_id)
        return [running, await launcher.is_running(instance.instance_id)]

    assert asyncio.run(scenario()) == [True, False]
    assert asyncio.run(launcher.is_running("instance-unknown")) is False
