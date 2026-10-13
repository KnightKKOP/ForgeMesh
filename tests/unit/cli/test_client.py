"""HTTP 客户端单元测试：请求头、错误响应到消息与退出码的映射、重定向、连接失败与翻页。

错误响应有两种：控制平面与网关的错误信封，以及不是错误信封的响应（如网关前面的反向代理
返回的纯文本 ``429`` / ``502``，和不提供 API 的地址返回的任何内容）。
"""

from __future__ import annotations

import pytest
from forgemesh.cli.client import ApiClient
from forgemesh.cli.errors import ApiError, CliError, ConnectionFailure, ExitCode

from .fakes import SERVICES, FakeApi, document, error_body, unused_url

PATH = f"{SERVICES}/llama"


def client_for(url: str, *, api_key: str | None = None, tenant: str | None = None) -> ApiClient:
    return ApiClient(url, api_key=api_key, tenant=tenant, correlation_id="cid-cli")


def get_error(api: FakeApi, *, api_key: str | None = "fmk_key") -> ApiError:
    with pytest.raises(ApiError) as raised:
        client_for(api.url, api_key=api_key).get_service("llama", "default")
    return raised.value


def test_requests_carry_the_key_the_tenant_and_the_correlation_id(api: FakeApi) -> None:
    api.add(document())

    client_for(api.url, api_key="fmk_key", tenant="acme").get_service("llama", "default")
    client_for(api.url).get_service("llama", "default")

    with_key, without = (call.headers for call in api.calls)
    assert with_key["authorization"] == "Bearer fmk_key"
    assert with_key["x-tenant-id"] == "acme"
    assert with_key["x-correlation-id"] == "cid-cli"
    assert with_key["accept"] == "application/json"
    assert with_key["user-agent"] == "forgemesh-cli"
    assert "authorization" not in without
    assert "x-tenant-id" not in without


def test_names_cannot_change_the_path_or_the_query(api: FakeApi) -> None:
    with pytest.raises(ApiError):
        client_for(api.url).get_service("a/b?namespace=other", None)

    assert api.log() == [f"GET {SERVICES}/a%2Fb%3Fnamespace%3Dother"]


def test_an_error_envelope_keeps_its_message_code_reason_and_correlation_id(api: FakeApi) -> None:
    details = {"reason": "ResourceVersionMismatch"}
    api.reply("GET", PATH, 409, error_body("CONFLICT", "version mismatch", "cid-1", details))

    error = get_error(api)

    assert (error.status, error.code, error.reason, error.exit_code) == (
        409,
        "CONFLICT",
        "ResourceVersionMismatch",
        ExitCode.CONFLICT,
    )
    assert error.message == (
        "version mismatch (HTTP 409 CONFLICT, reason ResourceVersionMismatch, correlation id cid-1)"
    )


def test_validation_errors_are_listed_one_per_line(api: FakeApi) -> None:
    errors = [
        {"loc": ["body", "spec", "resources"], "msg": "Field required", "type": "missing"},
        {"loc": [], "msg": "Input should be an object", "type": "model_type"},
    ]
    api.reply(
        "GET",
        PATH,
        400,
        error_body("BAD_REQUEST", "request validation failed", "c", {"errors": errors}),
    )

    error = get_error(api)

    assert error.exit_code == ExitCode.REJECTED
    assert error.message == (
        "request validation failed (HTTP 400 BAD_REQUEST, correlation id c)\n"
        "  body.spec.resources: Field required\n"
        "  -: Input should be an object"
    )


def test_an_envelope_without_a_correlation_id_shows_the_one_the_cli_sent(api: FakeApi) -> None:
    api.reply("GET", PATH, 403, {"error": {"code": "FORBIDDEN", "message": "write scope required"}})

    error = get_error(api)

    assert (error.exit_code, error.message) == (
        ExitCode.AUTH,
        "write scope required (HTTP 403 FORBIDDEN, correlation id cid-cli)",
    )


def test_the_gateway_rate_limit_keeps_its_code_and_retry_after(api: FakeApi) -> None:
    api.reply(
        "GET",
        PATH,
        429,
        error_body("RATE_LIMITED", "rate limit exceeded", "cid-gw"),
        headers={"Retry-After": "2"},
    )

    error = get_error(api)

    assert (error.exit_code, error.code, error.retry_after, error.transient) == (
        ExitCode.RATE_LIMITED,
        "RATE_LIMITED",
        2,
        True,
    )
    assert error.message == (
        "rate limit exceeded (HTTP 429 RATE_LIMITED, correlation id cid-gw); retry after 2 s"
    )


def test_a_plain_text_rate_limit_keeps_retry_after(api: FakeApi) -> None:
    api.reply("GET", PATH, 429, raw=b"Too Many Requests\n", headers={"Retry-After": "2"})

    error = get_error(api)

    assert (error.exit_code, error.code, error.retry_after, error.transient) == (
        ExitCode.RATE_LIMITED,
        None,
        2,
        True,
    )
    assert error.message == (
        "rate limited (HTTP 429 Too Many Requests) (correlation id cid-cli); retry after 2 s"
    )


def test_a_retry_after_that_is_not_a_number_of_seconds_is_ignored(api: FakeApi) -> None:
    api.reply("GET", PATH, 429, raw=b"", headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})

    error = get_error(api)

    assert error.retry_after is None
    assert "retry after" not in error.message


def test_the_gateway_without_a_control_plane_is_unavailable(api: FakeApi) -> None:
    api.reply(
        "GET", PATH, 502, error_body("UPSTREAM_UNAVAILABLE", "control plane unavailable", "cid-gw")
    )

    error = get_error(api)

    assert (error.exit_code, error.transient) == (ExitCode.UNAVAILABLE, True)
    assert error.message == (
        "control plane unavailable (HTTP 502 UPSTREAM_UNAVAILABLE, correlation id cid-gw)"
    )


def test_a_plain_text_bad_gateway_is_unavailable(api: FakeApi) -> None:
    api.reply("GET", PATH, 502, raw=b"Bad Gateway\n")

    error = get_error(api)

    assert (error.exit_code, error.transient) == (ExitCode.UNAVAILABLE, True)
    assert error.message == (
        "the server could not reach its upstream (HTTP 502 Bad Gateway) (correlation id cid-cli)"
    )


def test_a_control_plane_that_is_not_ready_is_unavailable(api: FakeApi) -> None:
    api.reply("GET", PATH, 503, error_body("REGISTRY_UNAVAILABLE", "registry is not ready", "c"))

    error = get_error(api)

    assert (error.exit_code, error.transient) == (ExitCode.UNAVAILABLE, True)


def test_the_gateway_route_not_found_does_not_mean_the_service_is_missing(api: FakeApi) -> None:
    """网关对没有路由的路径返回 ``ROUTE_NOT_FOUND``：地址错了，退出码不能是"资源不存在"。"""
    api.reply("GET", PATH, 404, error_body("ROUTE_NOT_FOUND", "route not found", "cid-gw"))

    error = get_error(api)

    assert (error.exit_code, error.not_found, error.code) == (
        ExitCode.ERROR,
        False,
        "ROUTE_NOT_FOUND",
    )
    assert error.message == (
        "route not found (HTTP 404 ROUTE_NOT_FOUND, correlation id cid-gw); check --api-url"
    )


def test_a_missing_service_is_not_found(api: FakeApi) -> None:
    api.reply("GET", PATH, 404, error_body("NOT_FOUND", "InferenceService 'llama' not found"))

    error = get_error(api)

    assert (error.exit_code, error.not_found) == (ExitCode.NOT_FOUND, True)
    assert error.message == (
        "InferenceService 'llama' not found (HTTP 404 NOT_FOUND, correlation id cid-server)"
    )


def test_a_404_that_is_not_from_the_api_does_not_mean_the_service_is_missing(
    api: FakeApi,
) -> None:
    """不提供 API 的地址返回纯文本 404：地址错了，退出码不能是"资源不存在"。"""
    api.reply("GET", PATH, 404, raw=b"404 page not found\n")

    error = get_error(api)

    assert (error.exit_code, error.not_found) == (ExitCode.ERROR, False)
    assert error.message == (
        "HTTP 404 Not Found: 404 page not found; the response is not from the ForgeMesh API, "
        "check --api-url (correlation id cid-cli)"
    )


def test_a_plain_server_error_quotes_the_first_line_of_the_body(api: FakeApi) -> None:
    api.reply("GET", PATH, 500, raw=b"Internal Server Error\ngoroutine 1 [running]:\n")

    error = get_error(api)

    assert (error.exit_code, error.message) == (
        ExitCode.SERVER_ERROR,
        "HTTP 500 Internal Server Error: Internal Server Error (correlation id cid-cli)",
    )


def test_a_long_plain_body_is_shortened(api: FakeApi) -> None:
    api.reply("GET", PATH, 500, raw=b"x" * 500)

    error = get_error(api)

    assert error.message == (
        f"HTTP 500 Internal Server Error: {'x' * 200}... (correlation id cid-cli)"
    )


def test_control_characters_from_the_server_do_not_reach_the_terminal(api: FakeApi) -> None:
    api.reply("GET", PATH, 400, error_body("BAD_REQUEST", "bad\x1b]0;owned\x07 name", "c\x1b"))

    error = get_error(api)

    assert error.message == "bad?]0;owned? name (HTTP 400 BAD_REQUEST, correlation id c?)"


def test_redirects_are_not_followed_so_the_key_stays_with_the_configured_url(
    api: FakeApi,
) -> None:
    elsewhere = FakeApi()
    try:
        api.reply("GET", PATH, 307, raw=b"", headers={"Location": f"{elsewhere.url}{PATH}"})

        error = get_error(api)
    finally:
        elsewhere.close()

    assert elsewhere.calls == []
    assert (error.status, error.exit_code) == (307, ExitCode.ERROR)
    assert error.message == (
        f"HTTP 307 Temporary Redirect: redirected to {elsewhere.url}{PATH}; the CLI does not "
        "follow redirects, set --api-url to the address that serves the API "
        "(correlation id cid-cli)"
    )


def test_a_missing_key_is_explained_only_when_no_key_was_sent(api: FakeApi) -> None:
    unauthenticated = error_body("UNAUTHENTICATED", "missing bearer credentials", "c")
    api.reply("GET", PATH, 401, unauthenticated)
    api.reply("GET", PATH, 401, unauthenticated)

    without_key = get_error(api, api_key=None)
    with_key = get_error(api, api_key="fmk_key")

    assert without_key.message.splitlines()[1:] == [
        "no API key is configured: set FORGEMESH_API_KEY or pass --api-key-file (a control "
        "plane in dev mode needs --tenant instead)"
    ]
    assert with_key.message == (
        "missing bearer credentials (HTTP 401 UNAUTHENTICATED, correlation id c)"
    )


def test_a_refused_connection_is_a_connection_failure() -> None:
    url = unused_url()

    with pytest.raises(ConnectionFailure) as raised:
        client_for(url).get_service("llama", "default")

    assert raised.value.exit_code == ExitCode.CONNECTION_FAILED
    assert raised.value.message.startswith(f"cannot reach the API at {url}: ")
    assert raised.value.message.endswith(
        "(check --api-url or FORGEMESH_API_URL, and that the gateway or control plane is running)"
    )


def test_a_success_response_that_is_not_json_is_an_error(api: FakeApi) -> None:
    api.reply("GET", PATH, 200, raw=b"<html>proxy login</html>")

    with pytest.raises(CliError) as raised:
        client_for(api.url).get_service("llama", "default")

    assert raised.value.exit_code == ExitCode.ERROR
    assert raised.value.message == (
        f"GET {PATH} at {api.url} answered HTTP 200 with a body that is not JSON; check --api-url"
    )


def test_a_resource_response_without_metadata_is_an_error(api: FakeApi) -> None:
    api.reply("GET", PATH, 200, {"items": []})

    with pytest.raises(CliError) as raised:
        client_for(api.url).get_service("llama", "default")

    assert raised.value.message == (
        f"the API at {api.url} returned an unexpected resource response; check --api-url"
    )


def test_list_follows_next_page_tokens_with_the_same_filter(api: FakeApi) -> None:
    for name in "abcde":
        api.add(document(name))

    items = list(client_for(api.url).list_services("default", page_size=2))

    assert [item["metadata"]["name"] for item in items] == list("abcde")
    assert api.log() == [
        f"GET {SERVICES}?limit=2&namespace=default",
        f"GET {SERVICES}?limit=2&namespace=default&pageToken=2",
        f"GET {SERVICES}?limit=2&namespace=default&pageToken=4",
    ]


def test_list_stops_when_the_api_repeats_a_page_token(api: FakeApi) -> None:
    api.reply("GET", SERVICES, 200, {"items": [], "count": 0, "nextPageToken": "t1"})
    api.reply("GET", SERVICES, 200, {"items": [], "count": 0, "nextPageToken": "t1"})

    with pytest.raises(CliError) as raised:
        list(client_for(api.url).list_services(None, page_size=10))

    assert raised.value.message == (
        "the API returned the same nextPageToken twice; the list is incomplete"
    )


@pytest.mark.parametrize(
    "page",
    [
        [],
        {"count": 0},
        {"items": "a"},
        {"items": [1]},
        {"items": [], "nextPageToken": ""},
        {"items": [], "nextPageToken": 2},
    ],
)
def test_list_rejects_a_page_of_another_shape(api: FakeApi, page: object) -> None:
    api.reply("GET", SERVICES, 200, page)

    with pytest.raises(CliError) as raised:
        list(client_for(api.url).list_services(None, page_size=10))

    assert raised.value.message == (
        f"the API at {api.url} returned an unexpected list response; check --api-url"
    )
