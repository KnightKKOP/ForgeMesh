"""列表接口分页（``limit`` / ``pageToken`` / ``nextPageToken``）单元测试。

覆盖 ``GET /api/v1/inference-services`` 与 ``GET /api/v1/resources``：按 ``(namespace, name)``
顺序翻页、最后一页不带 ``nextPageToken``、``limit`` 可以逐页变化、空 ``pageToken`` 等同于
省略，以及游标绑定租户与过滤条件、被篡改时返回 400 错误信封。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from forgemesh.controlplane.api import create_api_router
from forgemesh.controlplane.api.dependencies import get_registry, init_registry
from forgemesh.controlplane.api.errors import install_exception_handlers

SERVICES = "/api/v1/inference-services"
RESOURCES = "/api/v1/resources"
SPEC: dict[str, Any] = {
    "model": {"reference": "models/llama"},
    "runtime": {"provider": "vllm", "version": "0.6.3"},
    "resources": {"accelerator": {"type": "nvidia-gpu", "count": 1, "minMemoryGiB": 16}},
}


@pytest.fixture()
def client() -> TestClient:
    """带错误信封处理器的测试客户端（全新内存库）。"""
    init_registry(":memory:")
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_api_router())
    return TestClient(app)


def _create(client: TestClient, name: str, namespace: str = "default", tenant: str = "tenant-a"):
    response = client.post(
        SERVICES,
        json={
            "apiVersion": "forgemesh.io/v1alpha1",
            "kind": "InferenceService",
            "metadata": {"name": name, "namespace": namespace},
            "spec": SPEC,
        },
        headers={"X-Tenant-Id": tenant},
    )
    assert response.status_code == 201, response.text


def _list(client: TestClient, path: str = SERVICES, tenant: str = "tenant-a", **params: Any):
    return client.get(
        path, params=params, headers={"X-Tenant-Id": tenant, "X-Correlation-Id": "corr-1"}
    )


def _page(client: TestClient, path: str = SERVICES, **params: Any) -> dict[str, Any]:
    response = _list(client, path, **params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(page: dict[str, Any]) -> list[str]:
    return [item["metadata"]["name"] for item in page["items"]]


def _assert_rejected(response) -> None:
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert (error["code"], error["correlationId"]) == ("BAD_REQUEST", "corr-1")
    assert "pageToken" in error["message"]


@pytest.mark.parametrize("path", [SERVICES, RESOURCES])
def test_lists_page_through_all_services_in_order(client: TestClient, path: str) -> None:
    for namespace, name in [("b", "svc-1"), ("a", "svc-2"), ("a", "svc-1"), ("b", "svc-0")]:
        _create(client, name, namespace=namespace)
    _create(client, "svc-3", namespace=None)

    seen: list[tuple[str | None, str]] = []
    counts: list[int] = []
    params: dict[str, Any] = {"limit": 2}
    if path == RESOURCES:
        params["kind"] = "InferenceService"
    while True:
        page = _page(client, path, **params)
        seen.extend(
            (item["metadata"]["namespace"], item["metadata"]["name"]) for item in page["items"]
        )
        counts.append(page["count"])
        if "nextPageToken" not in page:
            break
        params["pageToken"] = page["nextPageToken"]

    assert seen == [(None, "svc-3"), ("a", "svc-1"), ("a", "svc-2"), ("b", "svc-0"), ("b", "svc-1")]
    assert counts == [2, 2, 1]


def test_a_full_last_page_has_no_next_page_token(client: TestClient) -> None:
    for i in range(3):
        _create(client, f"svc-{i}")

    first = _page(client, limit=2)
    last = _page(client, limit=1, pageToken=first["nextPageToken"])

    assert _names(last) == ["svc-2"]
    assert "nextPageToken" not in last
    assert "nextPageToken" not in _page(client, limit=3)


def test_the_limit_may_change_between_pages(client: TestClient) -> None:
    for i in range(4):
        _create(client, f"svc-{i}")
    first = _page(client, limit=1)

    rest = _page(client, limit=10, pageToken=first["nextPageToken"])

    assert _names(rest) == ["svc-1", "svc-2", "svc-3"]


def test_an_empty_page_token_starts_at_the_first_page(client: TestClient) -> None:
    for i in range(2):
        _create(client, f"svc-{i}")

    assert _names(_page(client, limit=1, pageToken="")) == ["svc-0"]


def test_list_items_match_the_single_resource_representation(client: TestClient) -> None:
    _create(client, "svc-0")
    asyncio.run(
        get_registry().update_status(
            "InferenceService",
            "default",
            "svc-0",
            "tenant-a",
            {"conditions": [{"type": "Ready", "status": "False", "reason": "Scheduling"}]},
            observed_generation=1,
        )
    )

    [item] = _page(client)["items"]
    fetched = client.get(
        f"{SERVICES}/svc-0", params={"namespace": "default"}, headers={"X-Tenant-Id": "tenant-a"}
    )

    assert item == fetched.json()


@pytest.mark.parametrize(
    ("tenant", "params"),
    [
        pytest.param("tenant-b", {"namespace": "default"}, id="other-tenant"),
        pytest.param("tenant-a", {"namespace": "other"}, id="other-namespace"),
        pytest.param("tenant-a", {}, id="all-namespaces"),
    ],
)
def test_service_page_tokens_are_bound_to_the_tenant_and_filters(
    client: TestClient, tenant: str, params: dict[str, Any]
) -> None:
    for owner in ("tenant-a", "tenant-b"):
        for namespace in ("default", "other"):
            for i in range(2):
                _create(client, f"svc-{i}", namespace=namespace, tenant=owner)
    token = _page(client, namespace="default", limit=1)["nextPageToken"]

    _assert_rejected(_list(client, tenant=tenant, limit=1, pageToken=token, **params))


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"kind": "Node"}, id="other-kind"),
        pytest.param({}, id="without-kind"),
    ],
)
def test_resource_page_tokens_are_bound_to_the_kind(
    client: TestClient, params: dict[str, Any]
) -> None:
    for i in range(2):
        _create(client, f"svc-{i}")
    token = _page(client, RESOURCES, kind="InferenceService", limit=1)["nextPageToken"]

    _assert_rejected(_list(client, RESOURCES, limit=1, pageToken=token, **params))


def test_listing_resources_without_kind_stays_empty(client: TestClient) -> None:
    _create(client, "svc-0")

    assert _page(client, RESOURCES) == {"items": [], "count": 0}


def _replace_last_character(token: str) -> str:
    return token[:-1] + ("B" if token.endswith("A") else "A")


def _replace_first_character(token: str) -> str:
    return ("B" if token.startswith("A") else "A") + token[1:]


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_replace_first_character, id="modified-payload"),
        pytest.param(_replace_last_character, id="modified-signature"),
        pytest.param(lambda token: token.partition(".")[0], id="unsigned"),
        pytest.param(lambda token: "not-a-token", id="garbage"),
    ],
)
def test_tampered_page_tokens_are_rejected(client: TestClient, tamper) -> None:
    for i in range(3):
        _create(client, f"svc-{i}")
    token = _page(client, limit=1)["nextPageToken"]

    _assert_rejected(_list(client, limit=1, pageToken=tamper(token)))
