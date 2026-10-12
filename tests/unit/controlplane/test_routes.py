"""workflows / decisions 运行时端点单元测试。

覆盖 ``POST /api/v1/workflows`` 与 ``POST /api/v1/decisions``（development-plan
5.4 / 5.5，对齐 ``api/openapi/openapi.yaml`` 契约）：

* workflows：成功受理（202 + WorkflowRun 形状）、幂等键复用（同 key 同 runId）、
  请求校验失败 400 错误信封、缺租户头 400、组件未注入 503、定义不存在 404；
* decisions：成功候选（200 + DecisionResponse 形状）、降级标记 degraded、
  全部 Provider 失败 503 错误信封（绝不默认放行）、Router 异常 500、
  租户头注入内部模型、缺租户头 400、校验失败 400。

``WorkflowRuntime`` / ``DecisionRouter`` 以 stub 注入（不依赖真实 Provider），
沿用 ``test_api.py`` 的 TestClient 模式。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from forgemesh.controlplane.api import create_api_router
from forgemesh.controlplane.api.dependencies import (
    init_decision_router,
    init_registry,
    init_workflow_runtime,
    reset_runtime_components,
)
from forgemesh.core.errors import NotFoundError
from forgemesh.decision import Attempt, DecisionCandidate, DecisionResult
from forgemesh.workflow.runtime import RunRef, WorkflowDefinition

TENANT_HEADERS = {"X-Tenant-Id": "tenant-a"}

#: stub runtime 固定返回的定义版本。
STUB_DEFINITION_VERSION = 3


class StubWorkflowRuntime:
    """最小 WorkflowRuntime stub：记录调用并模拟幂等复用语义。

    同一 ``idempotency_key`` 重复调用返回既有 :class:`RunRef`（不产生
    重复副作用），对齐 ``forgemesh.workflow.runtime`` 契约。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[WorkflowDefinition, dict[str, Any], str]] = []
        self._runs: dict[str, RunRef] = {}

    async def start(
        self,
        defn: WorkflowDefinition,
        inputs: dict[str, Any],
        idempotency_key: str,
    ) -> RunRef:
        self.calls.append((defn, dict(inputs), idempotency_key))
        if idempotency_key in self._runs:
            return self._runs[idempotency_key]
        ref = RunRef(
            run_id=f"run-{len(self._runs) + 1}", definition_version=STUB_DEFINITION_VERSION
        )
        self._runs[idempotency_key] = ref
        return ref


class StubDecisionRouter:
    """最小 DecisionRouter stub：记录请求并返回预置结果或抛出预置异常。"""

    def __init__(
        self,
        result: DecisionResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[Any] = []
        self._result = result
        self._exc = exc

    async def route(self, req: Any) -> DecisionResult:
        self.calls.append(req)
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _ok_result(degraded: bool = False) -> DecisionResult:
    """构造成功决策结果；``degraded=True`` 时首个 Provider 尝试失败。"""
    attempts = (
        [Attempt(provider="p1", ok=False, error="TimeoutError", latency_ms=1.0)] if degraded else []
    )
    attempts.append(Attempt(provider="p2", ok=True, latency_ms=2.0))
    candidate = DecisionCandidate(
        candidate_id="c1",
        action="inference.schedule",
        rationale=["首选可用"],
        confidence=0.9,
    )
    return DecisionResult(
        candidates=[candidate],
        chosen=candidate,
        success=True,
        status="OK",
        attempts=attempts,
    )


@pytest.fixture()
def client() -> TestClient:
    """构造带 API 路由的测试客户端（全新内存库，组件默认未注入）。"""
    init_registry(":memory:")
    reset_runtime_components()
    app = FastAPI()
    app.include_router(create_api_router())
    return TestClient(app)


# ------------------------------------------------------------------ #
# POST /api/v1/workflows
# ------------------------------------------------------------------ #


def test_start_workflow_accepted(client: TestClient) -> None:
    """成功受理：202 + WorkflowRun 形状（runId/state/definitionVersion）。"""
    runtime = StubWorkflowRuntime()
    init_workflow_runtime(runtime)
    resp = client.post(
        "/api/v1/workflows",
        json={"definition": "gpu-onboarding", "version": 3, "inputs": {"gpu": 1}},
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["runId"] == "run-1"
    assert body["state"] == "Pending"
    assert body["definitionVersion"] == STUB_DEFINITION_VERSION

    # runtime 收到正确的定义名 / 输入 / 租户命名空间化的幂等键。
    defn, inputs, idem_key = runtime.calls[0]
    assert defn.name == "gpu-onboarding"
    assert inputs == {"gpu": 1}
    assert idem_key.startswith("tenant-a:")


def test_start_workflow_idempotent_reuse(client: TestClient) -> None:
    """同一 idempotency_key 重复请求复用同一 runId（幂等语义由 runtime 保证）。"""
    init_workflow_runtime(StubWorkflowRuntime())
    payload = {"definition": "gpu-onboarding", "version": 3, "idempotency_key": "key-1"}
    first = client.post("/api/v1/workflows", json=payload, headers=TENANT_HEADERS)
    second = client.post("/api/v1/workflows", json=payload, headers=TENANT_HEADERS)
    assert first.status_code == second.status_code == 202
    assert first.json()["runId"] == second.json()["runId"]


def test_start_workflow_validation_error(client: TestClient) -> None:
    """非法请求体（version < 1）返回 400 统一错误信封。"""
    init_workflow_runtime(StubWorkflowRuntime())
    resp = client.post(
        "/api/v1/workflows",
        json={"definition": "gpu-onboarding", "version": 0},
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    assert error["correlationId"]
    assert error["details"]["errors"]


def test_start_workflow_missing_tenant_header(client: TestClient) -> None:
    """缺 X-Tenant-Id 头返回 401（dev 认证模式），不触达 runtime。"""
    init_workflow_runtime(StubWorkflowRuntime())
    resp = client.post("/api/v1/workflows", json={"definition": "gpu-onboarding", "version": 1})
    assert resp.status_code == 401, resp.text


def test_start_workflow_runtime_not_injected(client: TestClient) -> None:
    """WorkflowRuntime 未注入返回 503 错误信封（组装缺失诚实暴露）。"""
    resp = client.post(
        "/api/v1/workflows",
        json={"definition": "gpu-onboarding", "version": 1},
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "WORKFLOW_RUNTIME_UNAVAILABLE"


def test_start_workflow_definition_not_found(client: TestClient) -> None:
    """runtime 抛 NotFoundError（定义不存在）返回 404 错误信封。"""

    class _MissingDefRuntime(StubWorkflowRuntime):
        async def start(
            self,
            defn: WorkflowDefinition,
            inputs: dict[str, Any],
            idempotency_key: str,
        ) -> RunRef:
            raise NotFoundError(f"definition '{defn.name}' not found")

    init_workflow_runtime(_MissingDefRuntime())
    resp = client.post(
        "/api/v1/workflows",
        json={"definition": "nope", "version": 1},
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ------------------------------------------------------------------ #
# POST /api/v1/decisions
# ------------------------------------------------------------------ #


def test_decision_success(client: TestClient) -> None:
    """成功决策：200 + DecisionResponse 形状，租户头注入内部模型。"""
    router = StubDecisionRouter(result=_ok_result())
    init_decision_router(router)
    resp = client.post(
        "/api/v1/decisions",
        json={
            "question": "inference.schedule",
            "context": {"model": "llm-7b"},
            "requiredCapabilities": ["gpu.schedule"],
        },
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decisionId"].startswith("dec-")
    assert body["outcome"] == "inference.schedule"
    assert body["rationale"]
    assert body["degraded"] is False
    assert len(body["trace"]["attempts"]) == 1
    assert body["trace"]["candidates"][0]["candidate_id"] == "c1"

    # 内部模型：租户来自请求头，question→operation，能力列表并入 context。
    internal = router.calls[0]
    assert internal.tenant_id == "tenant-a"
    assert internal.operation == "inference.schedule"
    assert internal.context["requiredCapabilities"] == ["gpu.schedule"]
    assert internal.context["model"] == "llm-7b"


def test_decision_degraded_path(client: TestClient) -> None:
    """首个 Provider 失败后降级成功：degraded=True，attempts 记录降级过程。"""
    init_decision_router(StubDecisionRouter(result=_ok_result(degraded=True)))
    resp = client.post(
        "/api/v1/decisions",
        json={"question": "inference.schedule", "context": {}},
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is True
    attempts = body["trace"]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["provider"] == "p1" and attempts[0]["ok"] is False
    assert attempts[1]["provider"] == "p2" and attempts[1]["ok"] is True


def test_decision_no_candidate(client: TestClient) -> None:
    """全部 Provider 失败：503 错误信封（绝不默认放行），attempts 入 details。"""
    failed = DecisionResult(
        success=False,
        status="FAILED",
        error="no provider returned a valid candidate",
    )
    init_decision_router(StubDecisionRouter(result=failed))
    resp = client.post(
        "/api/v1/decisions",
        json={"question": "inference.schedule", "context": {}},
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["code"] == "DECISION_NO_CANDIDATE"
    assert error["correlationId"]
    assert "attempts" in error["details"]


def test_decision_router_exception(client: TestClient) -> None:
    """Router 抛异常：500 错误信封。"""
    init_decision_router(StubDecisionRouter(exc=RuntimeError("provider bus error")))
    resp = client.post(
        "/api/v1/decisions",
        json={"question": "inference.schedule", "context": {}},
        headers=TENANT_HEADERS,
    )
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"


def test_decision_missing_tenant_header(client: TestClient) -> None:
    """缺 X-Tenant-Id 头返回 401（dev 认证模式）。"""
    init_decision_router(StubDecisionRouter(result=_ok_result()))
    resp = client.post("/api/v1/decisions", json={"question": "inference.schedule", "context": {}})
    assert resp.status_code == 401, resp.text


def test_decision_validation_error(client: TestClient) -> None:
    """缺 question 返回 400 统一错误信封。"""
    init_decision_router(StubDecisionRouter(result=_ok_result()))
    resp = client.post("/api/v1/decisions", json={"context": {}}, headers=TENANT_HEADERS)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "BAD_REQUEST"
    assert error["details"]["errors"]
