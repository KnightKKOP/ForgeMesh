"""OpenAPI 契约漂移测试：控制平面实际提供的 API 与 ``api/openapi/openapi.yaml`` 一致。

除探针（``probes`` 标签）外，两个方向都检查：

* 静态：FastAPI 由路由生成的 OpenAPI 与契约有相同的路径与方法；每个操作的参数（位置、名称、
  是否必填、是否弃用，query / path 参数还比较类型）与请求体（是否必填、媒体类型）一致。应用的
  每个路由都进入生成的 Schema（框架自带的文档页除外），没有路由能绕过比较；
* 动态：按场景表调用每个操作（方法与路径取自契约），响应的状态码必须是契约为该操作声明的，
  响应体符合声明的 Schema（声明无正文时必须为空），声明的响应头存在，错误信封回显请求的
  ``X-Correlation-Id``，成功请求的请求体符合声明的请求体 Schema；契约为每个操作声明的每个
  状态码都至少有一个场景。

响应只做动态检查：FastAPI 生成的响应声明不含错误响应，比较它没有意义。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fastapi.routing
import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from forgemesh.controlplane.api.dependencies import (
    init_authenticator,
    init_decision_router,
    init_registry,
    init_workflow_runtime,
    reset_authenticator,
    reset_registry,
    reset_runtime_components,
)
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.auth import ApiKeyAuthenticator, ApiKeyEntry, hash_api_key
from forgemesh.controlplane.registry import SQLiteRegistry
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.decision import Attempt, DecisionCandidate, DecisionResult
from forgemesh.resource.inference import InferenceService
from forgemesh.workflow.runtime import RunRef, WorkflowDefinition
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "api" / "openapi" / "openapi.yaml"
CONTRACT: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
PROBES_TAG = "probes"
#: FastAPI 自带的文档页：由框架生成，不属于契约。
FRAMEWORK_ROUTES = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

API_KEY = "fmk_contract-tests"
TENANT = "tenant-a"
ENTRY = ApiKeyEntry(id="contract", sha256=hash_api_key(API_KEY), tenantId=TENANT, subject="ci")
CORRELATION_ID = "corr-contract"
NAMESPACE = {"namespace": "default"}
MERGE_PATCH = {"Content-Type": "application/merge-patch+json"}


# ------------------------------------------------------------------ #
# 契约与生成的 Schema
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class Operation:
    """一个操作；``parameters`` 已解析 ``$ref``，并入了路径级参数。"""

    method: str
    path: str
    spec: dict[str, Any]
    parameters: tuple[dict[str, Any], ...]

    @property
    def operation_id(self) -> str:
        return self.spec["operationId"]


def _resolve(document: dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        target: Any = document
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def _parameter_id(parameter: dict[str, Any]) -> tuple[str, str]:
    location = parameter["in"]
    name = parameter["name"]
    return location, name.lower() if location == "header" else name


def _operations(document: dict[str, Any]) -> dict[tuple[str, str], Operation]:
    """``(METHOD, path)`` → 操作，探针除外。"""
    operations: dict[tuple[str, str], Operation] = {}
    for path, item in document["paths"].items():
        shared = [_resolve(document, p) for p in item.get("parameters", [])]
        for method in HTTP_METHODS:
            spec = item.get(method)
            if spec is None or PROBES_TAG in spec.get("tags", []):
                continue
            own = [_resolve(document, p) for p in spec.get("parameters", [])]
            overridden = {_parameter_id(p) for p in own}
            parameters = [p for p in shared if _parameter_id(p) not in overridden] + own
            operations[(method.upper(), path)] = Operation(
                method.upper(), path, spec, tuple(parameters)
            )
    return operations


CONTRACT_OPERATIONS = _operations(CONTRACT)
OPERATIONS_BY_ID = {operation.operation_id: operation for operation in CONTRACT_OPERATIONS.values()}


def _value_types(document: dict[str, Any], schema: dict[str, Any]) -> tuple[str, ...]:
    schema = _resolve(document, schema)
    if "anyOf" in schema:
        types = {t for option in schema["anyOf"] for t in _value_types(document, option)}
    else:
        declared = schema.get("type", [])
        types = set(declared) if isinstance(declared, list) else {declared}
    return tuple(sorted(types - {"null"}))


def _parameter_signature(document: dict[str, Any], parameter: dict[str, Any]) -> tuple[Any, ...]:
    location, name = _parameter_id(parameter)
    # 请求头在线路上总是字符串（代码把 If-Match 声明为 list[str] 只为收集重复的请求头），
    # 因此只比较 query / path 参数的类型。
    types = () if location == "header" else _value_types(document, parameter.get("schema", {}))
    required = parameter.get("required", False)
    return location, name, required, parameter.get("deprecated", False), types


def _request_body_signature(
    document: dict[str, Any], operation: Operation
) -> tuple[bool, tuple[str, ...]] | None:
    body = operation.spec.get("requestBody")
    if body is None:
        return None
    body = _resolve(document, body)
    return body.get("required", False), tuple(sorted(body["content"]))


@pytest.fixture(scope="module")
def generated() -> dict[str, Any]:
    return create_app(db_path=":memory:").openapi()


def _served_routes(app: FastAPI) -> list[Any]:
    """应用实际匹配的路由。

    较新的 FastAPI 中 ``include_router`` 保留被包含的路由器而不是复制其路由，这时按生成 Schema
    时的同一种方式展开（同时得到包含时给定的前缀与 ``include_in_schema``）。
    """
    iter_route_contexts = getattr(fastapi.routing, "iter_route_contexts", None)
    if iter_route_contexts is None:
        return list(app.routes)
    return list(iter_route_contexts(app.routes))


@pytest.mark.contract
def test_every_route_is_part_of_the_generated_schema() -> None:
    """静态比较基于生成的 Schema：除框架文档页外，任何路由都不能被排除在它之外。"""
    for route in _served_routes(create_app(db_path=":memory:")):
        if isinstance(getattr(route, "original_route", route), APIRoute):
            assert route.include_in_schema, f"{route.path} is excluded from the generated schema"
        else:
            assert route.path in FRAMEWORK_ROUTES, f"unexpected route {route!r}"


@pytest.mark.contract
def test_served_operations_are_the_documented_ones(generated: dict[str, Any]) -> None:
    served = set(_operations(generated))
    documented = set(CONTRACT_OPERATIONS)

    assert sorted(served - documented) == [], "served but not documented"
    assert sorted(documented - served) == [], "documented but not served"


@pytest.mark.contract
@pytest.mark.parametrize(
    "key", sorted(CONTRACT_OPERATIONS), ids=lambda key: CONTRACT_OPERATIONS[key].operation_id
)
def test_parameters_and_request_bodies_match_the_contract(
    generated: dict[str, Any], key: tuple[str, str]
) -> None:
    served = _operations(generated)[key]
    documented = CONTRACT_OPERATIONS[key]

    assert sorted(_parameter_signature(generated, p) for p in served.parameters) == sorted(
        _parameter_signature(CONTRACT, p) for p in documented.parameters
    )
    assert _request_body_signature(generated, served) == _request_body_signature(
        CONTRACT, documented
    )


# ------------------------------------------------------------------ #
# 场景
# ------------------------------------------------------------------ #


class _Runtime:
    """工作流运行时替身：受理请求，或抛出给定的异常。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def start(
        self, defn: WorkflowDefinition, inputs: dict[str, Any], idempotency_key: str
    ) -> RunRef:
        if self.error is not None:
            raise self.error
        return RunRef(run_id="run-1", definition_version=1)


class _Router:
    """决策路由器替身：返回给定的结果，或抛出给定的异常。"""

    def __init__(
        self, result: DecisionResult | None = None, error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error

    async def route(self, request: Any) -> DecisionResult:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass
class Harness:
    client: TestClient
    registry: SQLiteRegistry


class Api:
    """绑定到一个契约操作的客户端：方法与路径模板取自契约；默认带凭证与关联 ID。"""

    def __init__(self, harness: Harness, operation_id: str) -> None:
        self.harness = harness
        self.operation = OPERATIONS_BY_ID[operation_id]

    def __call__(
        self,
        *,
        name: str = "svc-1",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: Any = None,
        content: bytes | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        request_headers = {"X-Correlation-Id": CORRELATION_ID}
        if authenticated:
            request_headers["Authorization"] = f"Bearer {API_KEY}"
        request_headers.update(headers or {})
        return self.harness.client.request(
            self.operation.method,
            self.operation.path.format(name=name),
            params=params,
            headers=request_headers,
            json=body,
            content=content,
        )

    def create(self, name: str) -> httpx.Response:
        response = Api(self.harness, "createInferenceService")(body=_service(name))
        assert response.status_code == 201, response.text
        return response

    def store(self, resource: InferenceService) -> None:
        asyncio.run(self.harness.registry.put(resource))


def _service(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "forgemesh.io/v1alpha1",
        "kind": "InferenceService",
        "metadata": {"name": name, "namespace": "default", "labels": {"app": name}},
        "spec": {
            "model": {"reference": "models/llama"},
            "runtime": {"provider": "vllm", "version": "0.6.3"},
            "resources": {"accelerator": {"type": "nvidia-gpu", "count": 1, "minMemoryGiB": 16}},
        },
    }


def _ready_service(name: str) -> InferenceService:
    """控制器已部署好的推理服务：Ready，带条件与实例地址。"""
    body = _service(name)
    body["metadata"].update(tenantId=TENANT, uid=f"uid-{name}", generation=1, observedGeneration=1)
    body["status"] = {
        "phase": "Ready",
        "observedGeneration": 1,
        "conditions": [
            {
                "type": "Ready",
                "status": "True",
                "reason": "InstancesReady",
                "lastTransitionTime": "2026-01-01T00:00:00Z",
            }
        ],
        "endpoints": [
            {"url": "http://10.0.0.1:8000", "protocol": "openai-chat", "instanceId": "inst-1"}
        ],
    }
    return InferenceService.model_validate(body)


def _candidate() -> DecisionCandidate:
    return DecisionCandidate(
        candidate_id="c1", action="inference.schedule", rationale=["first fit"], confidence=0.9
    )


Send = Callable[[Api], httpx.Response]


@dataclass(frozen=True)
class Scenario:
    operation_id: str
    status: int
    send: Send
    #: 错误信封中期望的 ``error.code``。
    code: str | None = None

    @property
    def id(self) -> str:
        return f"{self.operation_id}-{self.status}-{self.send.__name__.lstrip('_')}"


SCENARIOS: list[Scenario] = []


def scenario(operation_id: str, status: int, code: str | None = None) -> Callable[[Send], Send]:
    def register(send: Send) -> Send:
        SCENARIOS.append(Scenario(operation_id, status, send, code))
        return send

    return register


@scenario("listInferenceServices", 200)
def _page_with_a_next_page_token(api: Api) -> httpx.Response:
    api.create("svc-1")
    api.create("svc-2")
    return api(params={**NAMESPACE, "limit": 1})


@scenario("listInferenceServices", 400, "BAD_REQUEST")
def _tampered_page_token(api: Api) -> httpx.Response:
    return api(params={"pageToken": "not-a-page-token"})


@scenario("listInferenceServices", 503, "REGISTRY_UNAVAILABLE")
def _registry_not_initialised(api: Api) -> httpx.Response:
    reset_registry()
    return api()


@scenario("createInferenceService", 201)
def _created(api: Api) -> httpx.Response:
    return api(body=_service("svc-1"))


@scenario("createInferenceService", 400, "BAD_REQUEST")
def _create_without_spec(api: Api) -> httpx.Response:
    body = _service("svc-1")
    del body["spec"]
    return api(body=body)


@scenario("createInferenceService", 409, "CONFLICT")
def _name_taken(api: Api) -> httpx.Response:
    api.create("svc-1")
    return api(body=_service("svc-1"))


@scenario("getInferenceService", 200)
def _ready(api: Api) -> httpx.Response:
    api.store(_ready_service("svc-1"))
    return api(params=NAMESPACE)


@scenario("getInferenceService", 404, "NOT_FOUND")
def _get_missing(api: Api) -> httpx.Response:
    return api(params=NAMESPACE)


@scenario("replaceInferenceService", 200)
def _replaced(api: Api) -> httpx.Response:
    created = api.create("svc-1")
    document = created.json()
    document["spec"]["runtime"]["version"] = "0.7.0"
    return api(params=NAMESPACE, headers={"If-Match": created.headers["ETag"]}, body=document)


@scenario("replaceInferenceService", 400, "BAD_REQUEST")
def _unknown_top_level_field(api: Api) -> httpx.Response:
    document = api.create("svc-1").json()
    document["replicas"] = 3
    return api(params=NAMESPACE, body=document)


@scenario("replaceInferenceService", 404, "NOT_FOUND")
def _replace_missing(api: Api) -> httpx.Response:
    return api(params=NAMESPACE, body=_service("svc-1"))


@scenario("replaceInferenceService", 409, "CONFLICT")
def _stale_resource_version(api: Api) -> httpx.Response:
    document = api.create("svc-1").json()
    document["metadata"]["resourceVersion"] = "999"
    return api(params=NAMESPACE, body=document)


@scenario("patchInferenceService", 200)
def _patched(api: Api) -> httpx.Response:
    etag = api.create("svc-1").headers["ETag"]
    patch = json.dumps({"metadata": {"labels": {"team": "search"}}}).encode()
    return api(params=NAMESPACE, headers={**MERGE_PATCH, "If-Match": etag}, content=patch)


@scenario("patchInferenceService", 400, "BAD_REQUEST")
def _patch_not_an_object(api: Api) -> httpx.Response:
    api.create("svc-1")
    return api(params=NAMESPACE, headers=MERGE_PATCH, content=b"[]")


@scenario("patchInferenceService", 404, "NOT_FOUND")
def _patch_missing(api: Api) -> httpx.Response:
    return api(params=NAMESPACE, headers=MERGE_PATCH, content=b"{}")


@scenario("patchInferenceService", 409, "CONFLICT")
def _stale_if_match(api: Api) -> httpx.Response:
    api.create("svc-1")
    return api(params=NAMESPACE, headers={**MERGE_PATCH, "If-Match": '"stale"'}, content=b"{}")


@scenario("patchInferenceService", 415, "UNSUPPORTED_MEDIA_TYPE")
def _plain_json_patch(api: Api) -> httpx.Response:
    api.create("svc-1")
    return api(params=NAMESPACE, body={"metadata": {"labels": {"team": "search"}}})


@scenario("deleteInferenceService", 204)
def _deleted(api: Api) -> httpx.Response:
    api.create("svc-1")
    return api(params=NAMESPACE)


@scenario("deleteInferenceService", 202)
def _terminating(api: Api) -> httpx.Response:
    api.store(_ready_service("svc-1"))
    return api(params=NAMESPACE)


@scenario("deleteInferenceService", 404, "NOT_FOUND")
def _delete_missing(api: Api) -> httpx.Response:
    return api(params=NAMESPACE)


@scenario("deleteInferenceService", 409, "CONFLICT")
def _delete_raced_with_a_write(api: Api) -> httpx.Response:
    async def racing_delete(*args: Any, **kwargs: Any) -> Any:
        raise ConflictError("request_delete raced with concurrent write")

    api.create("svc-1")
    api.harness.registry.request_delete = racing_delete
    return api(params=NAMESPACE)


WORKFLOW_RUN = {"definition": "gpu-onboarding", "version": 1, "inputs": {"gpu": 1}}


@scenario("startWorkflow", 202)
def _accepted(api: Api) -> httpx.Response:
    init_workflow_runtime(_Runtime())
    return api(body=WORKFLOW_RUN)


@scenario("startWorkflow", 400, "BAD_REQUEST")
def _version_zero(api: Api) -> httpx.Response:
    init_workflow_runtime(_Runtime())
    return api(body={**WORKFLOW_RUN, "version": 0})


@scenario("startWorkflow", 404, "NOT_FOUND")
def _unknown_definition(api: Api) -> httpx.Response:
    init_workflow_runtime(_Runtime(NotFoundError("no such definition")))
    return api(body=WORKFLOW_RUN)


@scenario("startWorkflow", 500, "INTERNAL_ERROR")
def _runtime_failure(api: Api) -> httpx.Response:
    init_workflow_runtime(_Runtime(RuntimeError("runtime is down")))
    return api(body=WORKFLOW_RUN)


@scenario("startWorkflow", 503, "WORKFLOW_RUNTIME_UNAVAILABLE")
def _no_runtime(api: Api) -> httpx.Response:
    return api(body=WORKFLOW_RUN)


@scenario("requestDecision", 200)
def _decided(api: Api) -> httpx.Response:
    result = DecisionResult(
        candidates=[_candidate()],
        chosen=_candidate(),
        success=True,
        status="OK",
        attempts=[Attempt(provider="p1", ok=True, latency_ms=1.0)],
    )
    init_decision_router(_Router(result))
    return api(body={"question": "inference.schedule", "context": {"gpu": 1}})


@scenario("requestDecision", 400, "BAD_REQUEST")
def _missing_question(api: Api) -> httpx.Response:
    init_decision_router(_Router(error=AssertionError("the router must not be called")))
    return api(body={"context": {}})


@scenario("requestDecision", 500, "INTERNAL_ERROR")
def _router_failure(api: Api) -> httpx.Response:
    init_decision_router(_Router(error=RuntimeError("router is down")))
    return api(body={"question": "inference.schedule"})


@scenario("requestDecision", 503, "DECISION_ROUTER_UNAVAILABLE")
def _no_router(api: Api) -> httpx.Response:
    return api(body={"question": "inference.schedule"})


@scenario("requestDecision", 503, "DECISION_FALLBACK_REQUIRED")
def _fallback_candidate_for_a_high_risk_question(api: Api) -> httpx.Response:
    result = DecisionResult(
        candidates=[_candidate()],
        success=False,
        status="FALLBACK_REQUIRED",
        attempts=[
            Attempt(provider="p1", ok=False, error="TimeoutError"),
            Attempt(provider="p2", ok=True),
        ],
    )
    init_decision_router(_Router(result))
    return api(body={"question": "gpu.force-release"})


@scenario("listResources", 200)
def _resources_page(api: Api) -> httpx.Response:
    api.create("svc-1")
    api.create("svc-2")
    return api(params={"kind": "InferenceService", "limit": 1})


@scenario("listResources", 400, "BAD_REQUEST")
def _page_token_without_kind(api: Api) -> httpx.Response:
    return api(params={"pageToken": "not-a-page-token"})


def _without_credentials(api: Api) -> httpx.Response:
    return api(authenticated=False)


def _other_tenant_header(api: Api) -> httpx.Response:
    return api(headers={"X-Tenant-Id": "tenant-b"})


def _authenticator_not_initialised(api: Api) -> httpx.Response:
    reset_authenticator()
    return api()


#: 认证先于每个业务操作的其他检查，这三个场景对所有操作都适用。
SCENARIOS += [
    Scenario(operation_id, status, send, code)
    for operation_id in sorted(OPERATIONS_BY_ID)
    for status, send, code in (
        (401, _without_credentials, "UNAUTHENTICATED"),
        (403, _other_tenant_header, "FORBIDDEN"),
        (503, _authenticator_not_initialised, "AUTHENTICATOR_UNAVAILABLE"),
    )
]


# ------------------------------------------------------------------ #
# 动态检查
# ------------------------------------------------------------------ #


@pytest.fixture
def harness() -> Iterator[Harness]:
    """不运行 lifespan 的应用：没有控制器在后台改写资源，组件由测试逐个注入。"""
    registry = init_registry(":memory:")
    init_authenticator(ApiKeyAuthenticator([ENTRY]))
    reset_runtime_components()
    try:
        yield Harness(TestClient(create_app(db_path=":memory:")), registry)
    finally:
        reset_runtime_components()
        reset_authenticator()
        reset_registry()
        asyncio.run(registry.close())


def _assert_valid(instance: Any, schema: dict[str, Any], what: str) -> None:
    validator = Draft202012Validator({"components": CONTRACT["components"], **schema})
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    assert not errors, f"{what} violates the contract: " + "; ".join(
        f"{'/'.join(map(str, error.path))}: {error.message}" for error in errors
    )


def _media_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _check_response(operation: Operation, response: httpx.Response) -> None:
    status = str(response.status_code)
    assert status in operation.spec["responses"], (
        f"{operation.operation_id} does not document {status}"
    )
    documented = _resolve(CONTRACT, operation.spec["responses"][status])
    for header in documented.get("headers", {}):
        assert header in response.headers, f"response header {header} is missing"
    content = documented.get("content")
    if content is None:
        assert response.content == b""
        return
    media_type = _media_type(response.headers)
    assert media_type in content, f"undocumented response media type {media_type!r}"
    payload = response.json()
    _assert_valid(payload, content[media_type]["schema"], "the response body")
    if response.status_code >= 400:
        assert payload["error"]["correlationId"] == CORRELATION_ID


def _check_request(operation: Operation, request: httpx.Request) -> None:
    body = operation.spec.get("requestBody")
    if body is None:
        assert request.content == b""
        return
    content = _resolve(CONTRACT, body)["content"]
    media_type = _media_type(request.headers)
    assert media_type in content, f"undocumented request media type {media_type!r}"
    _assert_valid(json.loads(request.content), content[media_type]["schema"], "the request body")


@pytest.mark.contract
@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: case.id)
def test_responses_match_the_contract(case: Scenario, harness: Harness) -> None:
    api = Api(harness, case.operation_id)

    response = case.send(api)

    assert response.status_code == case.status, response.text
    if case.code is not None:
        assert response.json()["error"]["code"] == case.code
    _check_response(api.operation, response)
    if response.is_success:
        _check_request(api.operation, response.request)


@pytest.mark.contract
@pytest.mark.parametrize("operation_id", sorted(OPERATIONS_BY_ID))
def test_every_documented_status_code_is_exercised(operation_id: str) -> None:
    documented = {int(status) for status in OPERATIONS_BY_ID[operation_id].spec["responses"]}
    exercised = {case.status for case in SCENARIOS if case.operation_id == operation_id}

    assert sorted(exercised) == sorted(documented)
