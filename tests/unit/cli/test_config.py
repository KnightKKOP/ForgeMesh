"""CLI 配置单元测试：API 地址与 key 的来源、优先级与校验，错误消息不包含凭证。"""

from __future__ import annotations

from pathlib import Path

import pytest
from forgemesh.cli.config import (
    API_KEY_ENV,
    API_URL_ENV,
    DEFAULT_API_URL,
    check_namespace,
    check_tenant,
    resolve_api_key,
    resolve_api_url,
)
from forgemesh.cli.errors import ExitCode, UsageError

KEY = "fmk_" + "A1b2-_" * 7
NOT_A_URL = "--api-url must be an http or https URL such as http://127.0.0.1:8081"
NOT_A_KEY = (
    "does not hold a single API key: expected one line with the key printed by "
    "`python -m forgemesh.controlplane.keygen`"
)


def test_the_default_url_is_the_gateway_on_this_host() -> None:
    assert resolve_api_url(None, {}) == DEFAULT_API_URL == "http://127.0.0.1:8081"
    assert resolve_api_url(None, {API_URL_ENV: "  "}) == DEFAULT_API_URL


def test_the_option_takes_precedence_over_the_environment() -> None:
    environ = {API_URL_ENV: "http://127.0.0.1:8080"}

    assert resolve_api_url(None, environ) == "http://127.0.0.1:8080"
    assert resolve_api_url("https://forgemesh.example.com/", environ) == (
        "https://forgemesh.example.com"
    )


def test_the_path_prefix_of_a_reverse_proxy_is_kept() -> None:
    assert resolve_api_url("https://example.com/forgemesh/", {}) == "https://example.com/forgemesh"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("127.0.0.1:8081", NOT_A_URL),
        ("localhost:8081", NOT_A_URL),
        ("ftp://example.com", NOT_A_URL),
        ("http://", NOT_A_URL),
        (
            "http://user:s3cret@example.com",
            "--api-url must not contain credentials; use --api-key-file",
        ),
        ("http://example.com/?limit=1", "--api-url must not contain a query or a fragment"),
        ("http://example.com/#top", "--api-url must not contain a query or a fragment"),
        ("http://example.com:99999", "--api-url has an invalid port"),
        ("http://example.com:http", "--api-url has an invalid port"),
        (
            "http://example.com/api/v1/",
            "--api-url is the base URL of the API; remove the trailing /api/v1",
        ),
    ],
)
def test_invalid_urls_are_rejected(value: str, message: str) -> None:
    with pytest.raises(UsageError) as raised:
        resolve_api_url(value, {})

    assert (raised.value.exit_code, raised.value.message) == (ExitCode.USAGE, message)


def test_an_invalid_url_in_the_environment_is_named_but_not_echoed() -> None:
    with pytest.raises(UsageError) as raised:
        resolve_api_url(None, {API_URL_ENV: "http://admin:s3cret@example.com"})

    assert raised.value.message == (
        "FORGEMESH_API_URL must not contain credentials; use --api-key-file"
    )


def test_the_key_file_takes_precedence_and_surrounding_whitespace_is_ignored(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text(f"  {KEY}\n", encoding="utf-8")

    assert resolve_api_key(str(key_file), {API_KEY_ENV: "fmk_from_the_environment"}) == KEY


def test_without_a_file_the_key_comes_from_the_environment() -> None:
    assert resolve_api_key(None, {API_KEY_ENV: f" {KEY}\n"}) == KEY
    assert resolve_api_key(None, {API_KEY_ENV: "  "}) is None
    assert resolve_api_key(None, {}) is None


@pytest.mark.parametrize("content", ["", "\n  \n"])
def test_an_empty_key_file_is_rejected(tmp_path: Path, content: str) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text(content, encoding="utf-8")

    with pytest.raises(UsageError) as raised:
        resolve_api_key(str(key_file), {API_KEY_ENV: KEY})

    assert raised.value.message == f"the API key file {key_file} is empty"


def test_an_unreadable_key_file_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(UsageError) as missing_file:
        resolve_api_key(str(missing), {})
    with pytest.raises(UsageError) as directory:
        resolve_api_key(str(tmp_path), {})

    assert missing_file.value.message == (
        f"cannot read the API key file {missing}: No such file or directory"
    )
    assert directory.value.message == f"cannot read the API key file {tmp_path}: Is a directory"


def test_a_key_file_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_bytes(b"\xff\xfefmk_key")

    with pytest.raises(UsageError) as raised:
        resolve_api_key(str(key_file), {})

    assert raised.value.message == f"the API key file {key_file} is not UTF-8 text"


@pytest.mark.parametrize(
    "content",
    [
        "fmk_secret1 fmk_secret2",
        "fmk_secret1\nfmk_secret2\n",
        '{"key": "fmk_secret1"}',
        "Bearer fmk_secret1",
        "fmk_secret1ä",
    ],
)
def test_a_key_file_with_anything_but_one_key_is_rejected_without_echoing_it(
    tmp_path: Path, content: str
) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text(content, encoding="utf-8")

    with pytest.raises(UsageError) as raised:
        resolve_api_key(str(key_file), {})

    assert raised.value.message == f"the API key file {key_file} {NOT_A_KEY}"


def test_an_invalid_key_in_the_environment_is_rejected_without_echoing_it() -> None:
    with pytest.raises(UsageError) as raised:
        resolve_api_key(None, {API_KEY_ENV: "fmk_secret1\r\nX-Injected: 1"})

    assert raised.value.message == f"FORGEMESH_API_KEY {NOT_A_KEY}"


@pytest.mark.parametrize("value", ["acme", "team-a", "a.b_c~1"])
def test_visible_ascii_tenants_are_accepted(value: str) -> None:
    assert check_tenant(value, "--tenant") == value


@pytest.mark.parametrize("value", ["", "team a", "team-a\n", "équipe"])
def test_tenants_that_cannot_be_sent_as_a_header_are_rejected(value: str) -> None:
    with pytest.raises(UsageError) as raised:
        check_tenant(value, "--tenant")

    assert raised.value.message == (
        "--tenant must be a non-empty string of visible ASCII characters"
    )


def test_an_empty_namespace_is_rejected() -> None:
    assert check_namespace("team-a", "--namespace") == "team-a"
    with pytest.raises(UsageError) as raised:
        check_namespace("", "--namespace")

    assert raised.value.message == (
        "--namespace must not be empty; omit it for resources without a namespace"
    )
