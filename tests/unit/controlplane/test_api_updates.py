"""推理服务更新端点（``PUT`` / ``PATCH``）与 ``ETag`` / ``If-Match`` 单元测试。

覆盖：整体替换与 JSON Merge Patch 的写入结果、generation 只随 spec 递增、服务端维护的
字段被忽略、不可变字段、uid / resourceVersion / If-Match 前置条件与并发冲突、Terminating
拒绝更新、媒体类型，以及合并补丁与 If-Match 解析本身。
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from forgemesh.controlplane.api import create_api_router
from forgemesh.controlplane.api.dependencies import get_registry, init_registry
from forgemesh.controlplane.api.errors import ApiError, install_exception_handlers
from forgemesh.controlplane.api.updates import IfMatch, apply_merge_patch, parse_if_match
from forgemesh.controlplane.registry import ConflictReason, SQLiteRegistry
from forgemesh.core.errors import ConflictError
from forgemesh.core.objects import ObjectMeta
from forgemesh.resource.inference import (
    AcceleratorSpec,
    InferencePhase,
    InferenceService,
    InferenceServiceSpec,
    InferenceServiceStatus,
    ModelRef,
    ResourcesSpec,
    RuntimeRef,
)

TENANT_HEADERS = {"X-Tenant-Id": "tenant-a"}
PATH = "/api/v1/inference-services/svc-1"
PARAMS = {"namespace": "default"}
MERGE_PATCH = "application/merge-patch+json"
SPEC: dict[str, Any] = {
    "model": {"reference": "models/llama"},
    "runtime": {"provider": "vllm", "version": "0.6.3"},
    "resources": {"accelerator": {"type": "nvidia-gpu", "count": 1, "minMemoryGiB": 16}},
}


@pytest.fixture()
def client() -> TestClient:
    """带错误信封处理器的测试客户端（全新内存库），已创建资源 ``default/svc-1``。"""
    init_registry(":memory:")
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_api_router())
    test_client = TestClient(app)
    created = test_client.post(
        "/api/v1/inference-services",
        json={
            "apiVersion": "forgemesh.io/v1alpha1",
            "kind": "InferenceService",
            "metadata": {
                "name": "svc-1",
                "namespace": "default",
                "labels": {"tier": "gold", "zone": "a"},
                "annotations": {"team": "search"},
            },
            "spec": SPEC,
        },
        headers=TENANT_HEADERS,
    )
    assert created.status_code == 201, created.text
    return test_client


def _get(client: TestClient) -> dict[str, Any]:
    response = client.get(PATH, params=PARAMS, headers=TENANT_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _put(client: TestClient, document: Any, **headers: str):
    return client.put(PATH, params=PARAMS, json=document, headers={**TENANT_HEADERS, **headers})


def _patch(client: TestClient, patch: Any, content_type: str = MERGE_PATCH, **headers: str):
    return client.patch(
        PATH,
        params=PARAMS,
        content=json.dumps(patch).encode(),
        headers={**TENANT_HEADERS, "Content-Type": content_type, **headers},
    )


def _error(response) -> tuple[str, Any]:
    error = response.json()["error"]
    return error["code"], (error.get("details") or {}).get("reason")


def _with_model(document: dict[str, Any], reference: str) -> dict[str, Any]:
    changed = copy.deepcopy(document)
    changed["spec"]["model"]["reference"] = reference
    return changed


def _set_status() -> None:
    """模拟控制器写入 status（递增 resourceVersion，不改 generation）。"""
    asyncio.run(
        get_registry().update_status(
            "InferenceService",
            "default",
            "svc-1",
            "tenant-a",
            {"conditions": [{"type": "Ready", "status": "False", "reason": "Scheduling"}]},
            observed_generation=1,
        )
    )


# ------------------------------------------------------------------ #
# PUT
# ------------------------------------------------------------------ #


def test_put_replaces_spec_labels_and_annotations(client: TestClient) -> None:
    document = _with_model(_get(client), "models/mistral")
    document["metadata"]["labels"] = {"zone": "b"}
    del document["metadata"]["annotations"]

    response = _put(client, document)

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["spec"]["model"]["reference"] == "models/mistral"
    assert (updated["metadata"]["labels"], updated["metadata"]["annotations"]) == (
        {"zone": "b"},
        {},
    )
    assert (updated["metadata"]["generation"], updated["metadata"]["observedGeneration"]) == (2, 0)
    assert updated["metadata"]["resourceVersion"] == "1"
    uid = updated["metadata"]["uid"]
    assert response.headers["ETag"] == f'"{uid}.1"'
    assert _get(client) == updated


def test_label_and_annotation_changes_keep_the_generation(client: TestClient) -> None:
    document = _get(client)
    document["metadata"]["labels"]["zone"] = "b"
    document["metadata"]["annotations"] = {"team": "ads"}

    updated = _put(client, document).json()

    assert updated["metadata"]["generation"] == 1
    assert updated["metadata"]["resourceVersion"] == "1"
    assert updated["metadata"]["labels"] == {"tier": "gold", "zone": "b"}


def test_put_without_changes_is_not_written(client: TestClient) -> None:
    before = client.get(PATH, params=PARAMS, headers=TENANT_HEADERS)
    document = before.json()
    document["spec"] = {key: SPEC[key] for key in ("model", "runtime", "resources")}

    response = _put(client, document)

    assert response.status_code == 200, response.text
    assert response.json() == before.json()
    assert response.headers["ETag"] == before.headers["ETag"]


def test_put_ignores_status_and_server_owned_fields(client: TestClient) -> None:
    document = _with_model(_get(client), "models/mistral")
    document["status"] = {"phase": "Ready", "observedGeneration": 9}
    document["conditions"] = [{"type": "Ready", "status": "True"}]
    document["finalizers"] = ["example.com/keep"]
    document["metadata"].update(generation=9, observedGeneration=9)

    updated = _put(client, document).json()

    assert (updated["metadata"]["generation"], updated["metadata"]["observedGeneration"]) == (2, 0)
    assert updated["status"]["phase"] == "Pending"
    assert (updated["conditions"], updated["finalizers"]) == ([], [])


def test_put_of_a_legacy_generation_zero_resource_counts_up_from_zero(client: TestClient) -> None:
    """1.1.0 之前创建的资源 generation 为 0：不迁移，第一次 spec 变化后为 1。"""
    registry = get_registry()
    legacy = InferenceService(
        metadata=ObjectMeta(name="legacy", namespace="default", tenant_id="tenant-a", uid="u-0"),
        spec=InferenceServiceSpec.model_validate(SPEC),
    )
    asyncio.run(registry.put(legacy))
    path = "/api/v1/inference-services/legacy"
    document = client.get(path, params=PARAMS, headers=TENANT_HEADERS).json()
    assert document["metadata"]["generation"] == 0

    updated = client.put(
        path, params=PARAMS, json=_with_model(document, "models/mistral"), headers=TENANT_HEADERS
    ).json()

    assert (updated["metadata"]["generation"], updated["metadata"]["observedGeneration"]) == (1, 0)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.pop("spec"), id="missing-spec"),
        pytest.param(lambda d: d["spec"].update(replicas=3), id="unknown-spec-field"),
        pytest.param(lambda d: d["metadata"].update(owner="me"), id="unknown-metadata-field"),
        pytest.param(lambda d: d["metadata"]["labels"].update(tier=1), id="non-string-label"),
        pytest.param(lambda d: d.update(description="x"), id="unknown-top-level-field"),
        pytest.param(lambda d: d.update(metadata="svc-1"), id="metadata-not-object"),
        pytest.param(lambda d: d.update(kind="Gadget"), id="kind"),
        pytest.param(lambda d: d.update(apiVersion="forgemesh.io/v2"), id="api-version"),
        pytest.param(lambda d: d["metadata"].update(name="svc-2"), id="name"),
        pytest.param(lambda d: d["metadata"].update(namespace="other"), id="namespace"),
        pytest.param(lambda d: d["metadata"].update(namespace=None), id="namespace-null"),
        pytest.param(lambda d: d["metadata"].update(tenantId="tenant-b"), id="tenant"),
        pytest.param(lambda d: d["metadata"].update(resourceVersion=0), id="numeric-version"),
    ],
)
def test_put_rejects_invalid_documents(client: TestClient, mutate) -> None:
    document = _with_model(_get(client), "models/mistral")
    mutate(document)

    response = _put(client, document)

    assert response.status_code == 400, response.text
    assert _error(response)[0] == "BAD_REQUEST"
    assert _get(client)["metadata"]["resourceVersion"] == "0"


def test_put_validation_errors_point_at_the_field(client: TestClient) -> None:
    document = _get(client)
    document["spec"]["replicas"] = 3

    error = _put(client, document).json()["error"]

    assert [item["loc"] for item in error["details"]["errors"]] == [["spec", "replicas"]]


def test_put_without_the_namespace_query_addresses_another_resource(client: TestClient) -> None:
    """资源在 default 命名空间：省略 namespace 查询参数指的是另一个（不存在的）资源。"""
    response = client.put(
        "/api/v1/inference-services/svc-1", json=_get(client), headers=TENANT_HEADERS
    )

    assert response.status_code == 404, response.text


def test_put_with_another_uid_is_a_conflict(client: TestClient) -> None:
    document = _with_model(_get(client), "models/mistral")
    document["metadata"]["uid"] = "uid-of-a-deleted-service"

    response = _put(client, document)

    assert response.status_code == 409, response.text
    assert _error(response) == ("CONFLICT", ConflictReason.UID)


def test_concurrent_puts_with_the_same_resource_version_conflict(client: TestClient) -> None:
    """两个客户端读到同一版本：先写的成功，后写的 409，不会覆盖前者。"""
    first = _get(client)
    second = copy.deepcopy(first)

    winner = _put(client, _with_model(first, "models/mistral"))
    loser = _put(client, _with_model(second, "models/qwen"))

    assert winner.status_code == 200, winner.text
    assert loser.status_code == 409, loser.text
    assert _error(loser) == ("CONFLICT", ConflictReason.RESOURCE_VERSION)
    assert _get(client)["spec"]["model"]["reference"] == "models/mistral"


def test_status_writes_also_invalidate_a_resource_version(client: TestClient) -> None:
    """status 与 spec 共用 resourceVersion：读取之后控制器写过 status，带旧版本的写入冲突。"""
    document = _get(client)
    _set_status()

    response = _put(client, _with_model(document, "models/mistral"))

    assert response.status_code == 409, response.text
    del document["metadata"]["resourceVersion"]
    assert _put(client, _with_model(document, "models/mistral")).status_code == 200


def test_if_match_must_match_the_current_etag(client: TestClient) -> None:
    current = client.get(PATH, params=PARAMS, headers=TENANT_HEADERS)
    etag = current.headers["ETag"]
    document = current.json()
    del document["metadata"]["resourceVersion"]

    stale = _put(client, _with_model(document, "models/a"), **{"If-Match": '"stale.0"'})
    weak = _put(client, _with_model(document, "models/b"), **{"If-Match": f"W/{etag}"})
    listed = _put(client, _with_model(document, "models/c"), **{"If-Match": f'"x", {etag}'})
    star = _put(client, _with_model(document, "models/d"), **{"If-Match": "*"})
    outdated = _put(client, _with_model(document, "models/e"), **{"If-Match": etag})

    assert (stale.status_code, weak.status_code) == (409, 409)
    assert _error(stale) == ("CONFLICT", ConflictReason.RESOURCE_VERSION)
    assert listed.status_code == 200, listed.text
    assert listed.headers["ETag"] != etag
    assert star.status_code == 200, star.text
    assert outdated.status_code == 409
    assert _get(client)["spec"]["model"]["reference"] == "models/d"


def test_if_match_of_a_re_created_resource_does_not_match(client: TestClient) -> None:
    """同名资源删除后重建：resourceVersion 从 0 重新计数，但 ETag 含 uid，旧 ETag 不匹配。"""
    old_etag = client.get(PATH, params=PARAMS, headers=TENANT_HEADERS).headers["ETag"]
    old = _get(client)
    assert client.delete(PATH, params=PARAMS, headers=TENANT_HEADERS).status_code == 204
    recreated = {key: old[key] for key in ("apiVersion", "kind", "spec")}
    recreated["metadata"] = {"name": "svc-1", "namespace": "default"}
    assert (
        client.post("/api/v1/inference-services", json=recreated, headers=TENANT_HEADERS)
    ).status_code == 201

    response = _patch(client, {"spec": {"availability": {"replicas": 2}}}, **{"If-Match": old_etag})

    assert response.status_code == 409, response.text
    assert _get(client)["metadata"]["resourceVersion"] == "0"


@pytest.mark.parametrize("value", ["abc", '"unterminated', '"a" "b"', "", ", ,"])
def test_malformed_if_match_is_rejected(client: TestClient, value: str) -> None:
    response = _put(client, _get(client), **{"If-Match": value})

    assert response.status_code == 400, response.text
    assert _error(response)[0] == "BAD_REQUEST"


def test_concurrent_status_writes_are_kept_and_the_update_is_retried(
    client: TestClient, monkeypatch
) -> None:
    """读取之后、写入之前控制器写了 status：重新读取再写，status 不被覆盖，更新仍然成功。"""
    registry = get_registry()
    reads: list[str] = []
    get_with_phase = SQLiteRegistry.get_with_phase

    async def racing_get_with_phase(self, *args):
        result = await get_with_phase(self, *args)
        reads.append(result[0].metadata.resourceVersion)
        if len(reads) == 1:
            await self.update_status(
                "InferenceService",
                "default",
                "svc-1",
                "tenant-a",
                {"conditions": [{"type": "Ready", "status": "False", "reason": "Scheduling"}]},
                observed_generation=1,
            )
        return result

    monkeypatch.setattr(SQLiteRegistry, "get_with_phase", racing_get_with_phase)

    response = _patch(client, {"spec": {"availability": {"replicas": 2}}})

    assert response.status_code == 200, response.text
    assert reads == ["0", "1"]
    stored = asyncio.run(registry.get("InferenceService", "default", "svc-1", "tenant-a"))
    assert stored.spec.availability.replicas == 2
    assert [condition.reason for condition in stored.status.conditions] == ["Scheduling"]
    assert stored.metadata.resourceVersion == "2"


def test_update_gives_up_when_the_resource_keeps_changing(client: TestClient, monkeypatch) -> None:
    calls: list[str | None] = []

    async def always_conflicting(self, *args, expected_version=None, **kwargs):
        calls.append(expected_version)
        raise ConflictError("changed", details={"reason": ConflictReason.RESOURCE_VERSION.value})

    monkeypatch.setattr(SQLiteRegistry, "update_spec", always_conflicting)

    response = _put(client, _with_model(_get(client), "models/mistral"))

    assert response.status_code == 409, response.text
    assert _error(response) == ("CONFLICT", ConflictReason.RESOURCE_VERSION)
    assert len(calls) == 5


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_terminating_resources_reject_updates(client: TestClient, method: str) -> None:
    registry = get_registry()
    ready = InferenceService(
        metadata=ObjectMeta(name="ready", namespace="default", tenant_id="tenant-a", uid="u-1"),
        spec=InferenceServiceSpec(
            model=ModelRef(reference="models/llama"),
            runtime=RuntimeRef(provider="vllm"),
            resources=ResourcesSpec(
                accelerator=AcceleratorSpec(type="nvidia-gpu", minMemoryGiB=16)
            ),
        ),
        status=InferenceServiceStatus(phase=InferencePhase.READY),
    )
    asyncio.run(registry.put(ready))
    path = "/api/v1/inference-services/ready"
    document = client.get(path, params=PARAMS, headers=TENANT_HEADERS).json()
    assert client.delete(path, params=PARAMS, headers=TENANT_HEADERS).status_code == 202

    if method == "PUT":
        response = client.put(
            path,
            params=PARAMS,
            json=_with_model(document, "models/mistral"),
            headers=TENANT_HEADERS,
        )
    else:
        response = client.patch(
            path,
            params=PARAMS,
            content=b'{"metadata": {"labels": {"tier": "gold"}}}',
            headers={**TENANT_HEADERS, "Content-Type": MERGE_PATCH},
        )

    assert response.status_code == 409, response.text
    assert _error(response) == ("CONFLICT", ConflictReason.TERMINATING)


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_updates_of_missing_or_foreign_resources_are_not_found(
    client: TestClient, method: str
) -> None:
    document = _get(client)
    requests = [
        ("/api/v1/inference-services/nope", TENANT_HEADERS),
        (PATH, {"X-Tenant-Id": "tenant-b"}),
    ]
    for path, headers in requests:
        if method == "PUT":
            response = client.put(path, params=PARAMS, json=document, headers=headers)
        else:
            response = client.patch(
                path,
                params=PARAMS,
                content=b"{}",
                headers={**headers, "Content-Type": MERGE_PATCH},
            )
        assert response.status_code == 404, response.text
        assert _error(response)[0] == "NOT_FOUND"
    assert _get(client)["metadata"]["resourceVersion"] == "0"


# ------------------------------------------------------------------ #
# PATCH
# ------------------------------------------------------------------ #


def test_patch_merges_into_the_current_resource(client: TestClient) -> None:
    response = _patch(
        client,
        {
            "metadata": {"labels": {"zone": None, "team": "ads"}},
            "spec": {"availability": {"replicas": 2}},
        },
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["metadata"]["labels"] == {"tier": "gold", "team": "ads"}
    assert updated["metadata"]["annotations"] == {"team": "search"}
    assert updated["spec"]["availability"] == {"replicas": 2}
    assert updated["spec"]["model"] == {"reference": "models/llama", "digest": None}
    assert updated["metadata"]["generation"] == 2
    assert response.headers["ETag"] == f'"{updated["metadata"]["uid"]}.1"'


def test_patch_of_labels_only_keeps_the_generation(client: TestClient) -> None:
    updated = _patch(client, {"metadata": {"annotations": None}}).json()

    assert updated["metadata"]["annotations"] == {}
    assert (updated["metadata"]["generation"], updated["metadata"]["resourceVersion"]) == (1, "1")


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({}, id="empty"),
        pytest.param({"status": {"phase": "Ready"}}, id="status"),
        pytest.param({"metadata": {"generation": 5, "observedGeneration": 5}}, id="generation"),
        pytest.param({"spec": {"availability": {"replicas": 1}}}, id="default-value"),
    ],
)
def test_patch_without_effective_changes_is_not_written(client: TestClient, patch) -> None:
    before = _get(client)

    response = _patch(client, patch)

    assert response.status_code == 200, response.text
    assert response.json() == before


def test_patch_resource_version_is_a_precondition(client: TestClient) -> None:
    """补丁中给出的 resourceVersion 是前置条件；不给时合并到最新的资源上。"""
    _set_status()

    stale = _patch(client, {"metadata": {"resourceVersion": "0"}, "spec": {"model": None}})
    pinned = _patch(
        client, {"metadata": {"resourceVersion": "1"}, "spec": {"availability": {"replicas": 2}}}
    )
    unpinned = _patch(client, {"spec": {"availability": {"replicas": 3}}})

    assert stale.status_code == 409, stale.text
    assert _error(stale) == ("CONFLICT", ConflictReason.RESOURCE_VERSION)
    assert pinned.status_code == 200, pinned.text
    assert unpinned.status_code == 200, unpinned.text
    assert unpinned.json()["metadata"]["resourceVersion"] == "3"
    assert unpinned.json()["spec"]["availability"] == {"replicas": 3}


@pytest.mark.parametrize(
    ("patch", "status", "code"),
    [
        pytest.param({"kind": "Gadget"}, 400, "BAD_REQUEST", id="kind"),
        pytest.param({"metadata": {"name": "svc-2"}}, 400, "BAD_REQUEST", id="name"),
        pytest.param({"metadata": {"namespace": "other"}}, 400, "BAD_REQUEST", id="namespace"),
        pytest.param({"metadata": {"tenantId": "tenant-b"}}, 400, "BAD_REQUEST", id="tenant"),
        pytest.param({"spec": None}, 400, "BAD_REQUEST", id="remove-spec"),
        pytest.param({"spec": {"resources": None}}, 400, "BAD_REQUEST", id="remove-resources"),
        pytest.param({"lables": {"tier": "x"}}, 400, "BAD_REQUEST", id="unknown-field"),
        pytest.param({"metadata": {"uid": "other"}}, 409, "CONFLICT", id="uid"),
    ],
)
def test_patch_rejects_invalid_results(client: TestClient, patch, status: int, code: str) -> None:
    response = _patch(client, patch)

    assert response.status_code == status, response.text
    assert _error(response)[0] == code
    assert _get(client)["metadata"]["resourceVersion"] == "0"


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", ""])
def test_patch_requires_the_merge_patch_media_type(client: TestClient, content_type: str) -> None:
    response = _patch(client, {"metadata": {"labels": None}}, content_type=content_type)

    assert response.status_code == 415, response.text
    assert _error(response)[0] == "UNSUPPORTED_MEDIA_TYPE"
    assert response.headers["Accept-Patch"] == MERGE_PATCH


def test_patch_media_type_may_carry_parameters(client: TestClient) -> None:
    response = _patch(
        client, {"metadata": {"labels": None}}, content_type="Application/Merge-Patch+JSON; q=1"
    )

    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["labels"] == {}


@pytest.mark.parametrize("content", [b"[]", b'"labels"', b"null", b"{", b""])
def test_patch_must_be_a_json_object(client: TestClient, content: bytes) -> None:
    response = client.patch(
        PATH,
        params=PARAMS,
        content=content,
        headers={**TENANT_HEADERS, "Content-Type": MERGE_PATCH},
    )

    assert response.status_code == 400, response.text
    assert _error(response)[0] == "BAD_REQUEST"


# ------------------------------------------------------------------ #
# 创建与读取
# ------------------------------------------------------------------ #


def test_create_and_get_return_the_etag(client: TestClient) -> None:
    created = client.post(
        "/api/v1/inference-services",
        json={"kind": "InferenceService", "metadata": {"name": "svc-2"}, "spec": SPEC},
        headers=TENANT_HEADERS,
    )
    fetched = client.get("/api/v1/inference-services/svc-2", headers=TENANT_HEADERS)

    uid = created.json()["metadata"]["uid"]
    assert created.headers["ETag"] == fetched.headers["ETag"] == f'"{uid}.0"'


def test_create_rejects_unknown_top_level_fields(client: TestClient) -> None:
    response = client.post(
        "/api/v1/inference-services",
        json={
            "kind": "InferenceService",
            "metadata": {"name": "svc-2"},
            "spec": SPEC,
            "lables": {"tier": "gold"},
        },
        headers=TENANT_HEADERS,
    )

    assert response.status_code == 400, response.text
    assert "lables" in response.json()["error"]["message"]


def test_create_ignores_controller_owned_top_level_fields(client: TestClient) -> None:
    """GET 的响应可以原样用于创建：status / conditions / finalizers 被忽略。"""
    document = _get(client)
    document["metadata"]["name"] = "copy"

    response = client.post("/api/v1/inference-services", json=document, headers=TENANT_HEADERS)

    assert response.status_code == 201, response.text
    assert response.json()["finalizers"] == []


# ------------------------------------------------------------------ #
# 合并补丁与 If-Match 解析
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("target", "patch", "expected"),
    [
        ({"a": "b"}, {"a": "c"}, {"a": "c"}),
        ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
        ({"a": "b"}, {"a": None}, {}),
        ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
        ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
        ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
        ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
        ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
        (["a", "b"], ["c", "d"], ["c", "d"]),
        ({"a": "b"}, ["c"], ["c"]),
        ({"a": "foo"}, None, None),
        ({"a": "foo"}, "bar", "bar"),
        ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
        ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
        ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
    ],
)
def test_merge_patch_follows_rfc_7386(target: Any, patch: Any, expected: Any) -> None:
    """RFC 7386 附录 A 的全部示例；入参不被修改。"""
    original = copy.deepcopy((target, patch))

    assert apply_merge_patch(target, patch) == expected
    assert (target, patch) == original


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (None, None),
        ([], None),
        (["*"], IfMatch(any=True, tags=frozenset())),
        (['"a"'], IfMatch(any=False, tags=frozenset({'"a"'}))),
        (['"a", W/"b" ,"c"'], IfMatch(any=False, tags=frozenset({'"a"', '"c"'}))),
        (['"a"', '"b"'], IfMatch(any=False, tags=frozenset({'"a"', '"b"'}))),
        ([', "a",'], IfMatch(any=False, tags=frozenset({'"a"'}))),
        (['W/"a"'], IfMatch(any=False, tags=frozenset())),
        (['""'], IfMatch(any=False, tags=frozenset({'""'}))),
    ],
)
def test_if_match_parsing(values: list[str] | None, expected: IfMatch | None) -> None:
    assert parse_if_match(values) == expected


@pytest.mark.parametrize("value", ["a", '"a', '"a" "b"', "", " ", ",", 'W/ "a"', '"a"b'])
def test_malformed_if_match_values(value: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        parse_if_match([value])

    assert excinfo.value.status_code == 400


def test_weak_tags_never_match() -> None:
    condition = parse_if_match(['W/"u.1"'])

    assert condition is not None
    assert not condition.matches('"u.1"')
