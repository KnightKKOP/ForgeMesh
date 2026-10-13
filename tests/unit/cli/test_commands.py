"""CLI 命令单元测试：经 :func:`forgemesh.cli.main.main` 调用本机上的假 API（见 ``fakes.py``）。

覆盖 ``apply``（创建、以读到的版本更新、不变、冲突重试与放弃、正在删除、并发创建、多文档、
租户与命名空间、标准输入、``list`` 输出的再次提交）、``get`` / ``describe`` / ``list``（输出格式、
翻页、命名空间规则）、``delete``（``204`` / ``202``）、``wait``（就绪、``Failed`` 立即失败、
observedGeneration、超时、暂时故障、``--for delete``、Ctrl-C），以及 key、公共选项与参数校验。
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from forgemesh.cli.commands import APPLY_ATTEMPTS
from forgemesh.cli.config import API_KEY_ENV, API_URL_ENV
from forgemesh.cli.errors import ExitCode
from forgemesh.cli.main import build_parser, main

from .fakes import (
    DROP,
    SERVICES,
    FakeApi,
    FakeClock,
    Result,
    document,
    error_body,
    resource,
    unused_url,
)

Cli = Callable[..., Result]
PATH = f"{SERVICES}/llama"
KEY = "fmk_" + "k" * 43
CONFLICT = error_body(
    "CONFLICT", "resourceVersion mismatch", details={"reason": "ResourceVersionMismatch"}
)
READY = {"phase": "Ready", "ready": ("True", "Reconciled"), "observed": 1}


def write_yaml(path: Path, *docs: dict[str, Any]) -> str:
    path.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
    return str(path)


def correlation_id(api: FakeApi) -> str:
    return api.calls[-1].headers["x-correlation-id"]


def test_apply_creates_a_missing_service_and_leaves_it_unchanged_the_second_time(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    path = write_yaml(tmp_path / "svc.yaml", document())

    first = cli("apply", "-f", path)
    second = cli("apply", "-f", path)

    assert (first.code, first.out, first.err) == (0, "InferenceService default/llama created\n", "")
    assert (second.code, second.out, second.err) == (
        0,
        "InferenceService default/llama unchanged\n",
        "",
    )
    assert api.log() == [
        f"GET {PATH}?namespace=default",
        f"POST {SERVICES}",
        f"GET {PATH}?namespace=default",
        f"PUT {PATH}?namespace=default",
    ]
    assert api.calls[1].body == document()
    assert api.services[("default", "llama")]["metadata"]["resourceVersion"] == "0"


def test_apply_replaces_the_service_with_the_version_it_read(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    """文件中的 resourceVersion 早已过时（控制器每次写 status 都递增版本），以读到的为准。"""
    api.add(document(), version="7", uid="uid-1", **READY)
    stale = document(labels={"zone": "b"}, resourceVersion="3")

    result = cli("apply", "-f", write_yaml(tmp_path / "svc.yaml", stale))

    assert (result.code, result.out) == (0, "InferenceService default/llama configured\n")
    put = api.calls[-1]
    assert (put.method, put.body["metadata"]) == (
        "PUT",
        {
            "name": "llama",
            "namespace": "default",
            "labels": {"zone": "b"},
            "resourceVersion": "7",
            "uid": "uid-1",
        },
    )
    stored = api.services[("default", "llama")]["metadata"]
    assert (stored["labels"], stored["resourceVersion"], stored["generation"]) == (
        {"zone": "b"},
        "8",
        1,
    )


def test_apply_reads_the_service_again_after_a_conflicting_write(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    api.add(document(), version="1")
    api.reply("PUT", PATH, 409, CONFLICT)

    result = cli("apply", "-f", write_yaml(tmp_path / "svc.yaml", document(labels={"zone": "b"})))

    assert (result.code, result.out, result.err) == (
        0,
        "InferenceService default/llama configured\n",
        "",
    )
    assert [call.method for call in api.calls] == ["GET", "PUT", "GET", "PUT"]


def test_apply_gives_up_when_the_service_keeps_changing(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    api.add(document())
    for _ in range(APPLY_ATTEMPTS):
        api.reply("PUT", PATH, 409, CONFLICT)
    path = write_yaml(tmp_path / "svc.yaml", document(labels={"zone": "b"}))

    result = cli("apply", "-f", path)

    assert result.code == ExitCode.CONFLICT
    assert result.err == (
        f"Error: InferenceService default/llama ({path}): the resource kept changing while it "
        f"was applied ({APPLY_ATTEMPTS} attempts); try again\n"
    )
    assert [call.method for call in api.calls] == ["GET", "PUT"] * APPLY_ATTEMPTS


def test_apply_to_a_service_that_is_being_deleted_says_what_to_do(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    api.add(document(), phase="Terminating")
    path = write_yaml(tmp_path / "svc.yaml", document(labels={"zone": "b"}))

    result = cli("apply", "-f", path)

    assert result.code == ExitCode.CONFLICT
    assert result.err == (
        f"Error: InferenceService default/llama ({path}): InferenceService 'llama' was not "
        "updated: Terminating (HTTP 409 CONFLICT, reason Terminating, correlation id "
        f"{correlation_id(api)})\n"
        "wait until it is deleted (forgemesh wait inferenceservice llama -n default --for delete) "
        "and apply again\n"
    )


def test_apply_updates_a_service_that_was_created_concurrently(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    """读取时还不存在，创建时已经存在（409）：重新读取后更新它。"""
    api.add(document())
    api.reply("GET", PATH, 404, error_body("NOT_FOUND", "InferenceService 'llama' not found"))

    result = cli("apply", "-f", write_yaml(tmp_path / "svc.yaml", document(labels={"zone": "b"})))

    assert (result.code, result.out) == (0, "InferenceService default/llama configured\n")
    assert [call.method for call in api.calls] == ["GET", "POST", "GET", "PUT"]


def test_apply_reports_a_failed_document_and_applies_the_others(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    path = write_yaml(tmp_path / "svcs.yaml", document("a"), document("b"), document("c"))
    invalid = {"loc": ["body", "spec", "resources"], "msg": "Field required", "type": "missing"}
    api.reply(
        "POST",
        SERVICES,
        400,
        error_body("BAD_REQUEST", "request validation failed", details={"errors": [invalid]}),
    )

    result = cli("apply", "-f", path)

    assert result.code == ExitCode.REJECTED
    assert result.out == "InferenceService default/b created\nInferenceService default/c created\n"
    assert result.err == (
        f"Error: InferenceService default/a ({path} (document 1)): request validation failed "
        "(HTTP 400 BAD_REQUEST, correlation id cid-server)\n"
        "  body.spec.resources: Field required\n"
    )


def test_apply_sends_the_tenant_of_each_document(api: FakeApi, cli: Cli, tmp_path: Path) -> None:
    """文档中的租户作为 X-Tenant-Id 发送：与 key 的租户不符时 API 拒绝（403），而不是写进
    key 的租户。"""
    path = write_yaml(tmp_path / "svcs.yaml", document("a", tenantId="team-a"), document("b"))

    result = cli("apply", "-f", path)
    mismatch = cli("apply", "-f", path, "--tenant", "team-b")

    assert result.code == 0
    assert [(call.method, call.headers.get("x-tenant-id")) for call in api.calls] == [
        ("GET", "team-a"),
        ("POST", "team-a"),
        ("GET", None),
        ("POST", None),
    ]
    assert (mismatch.code, mismatch.err) == (
        ExitCode.USAGE,
        f"Error: {path} (document 1): metadata.tenantId 'team-a' does not match --tenant "
        "'team-b'\n",
    )
    assert len(api.calls) == 4


def test_apply_reads_json_from_standard_input_and_fills_in_the_namespace(
    api: FakeApi, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = [document("a", namespace=None), document("b", namespace=None)]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(docs)))

    result = cli("apply", "-f", "-", "-n", "team-a")

    assert (result.code, result.out) == (
        0,
        "InferenceService team-a/a created\nInferenceService team-a/b created\n",
    )
    posted = [call.body["metadata"] for call in api.calls if call.method == "POST"]
    assert posted == [
        {"name": "a", "namespace": "team-a"},
        {"name": "b", "namespace": "team-a"},
    ]


@pytest.mark.parametrize("output", ["yaml", "json"])
def test_the_output_of_list_can_be_applied_again(
    api: FakeApi, cli: Cli, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    api.add(document("a"), version="4", **READY)
    api.add(document("b", labels={"zone": "b"}), uid="uid-2", version="2")
    listed = cli("list", "inferenceservices", "-o", output)
    monkeypatch.setattr("sys.stdin", io.StringIO(listed.out))

    result = cli("apply", "-f", "-")

    assert (result.code, result.out, result.err) == (
        0,
        "InferenceService default/a unchanged\nInferenceService default/b unchanged\n",
        "",
    )


def test_apply_checks_every_document_before_sending_anything(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    path = write_yaml(tmp_path / "svcs.yaml", document("a"), {**document("b"), "kind": "Workflow"})

    result = cli("apply", "-f", path)

    assert (result.code, result.out) == (ExitCode.USAGE, "")
    assert result.err == (
        f"Error: {path} (document 2): kind 'Workflow' is not supported; the CLI applies "
        "InferenceService\n"
    )
    assert api.calls == []


def test_get_prints_the_service_as_yaml_or_json(api: FakeApi, cli: Cli) -> None:
    service = api.add(document(), **READY)

    as_yaml = cli("get", "inferenceservice", "llama", "-n", "default")
    as_json = cli("get", "InferenceService", "llama", "-n", "default", "-o", "json")

    assert yaml.safe_load(as_yaml.out) == service
    assert json.loads(as_json.out) == service
    assert as_yaml.out.startswith("apiVersion: forgemesh.io/v1alpha1\nkind: InferenceService\n")


def test_get_without_a_namespace_looks_for_a_service_without_one(api: FakeApi, cli: Cli) -> None:
    api.add(document())

    result = cli("get", "inferenceservice", "llama")
    elsewhere = cli("get", "inferenceservice", "llama", "-n", "other")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == (
        f"Error: InferenceService 'llama' not found (HTTP 404 NOT_FOUND, correlation id "
        f"{api.calls[0].headers['x-correlation-id']})\n"
        "no --namespace was given, so this looked for an InferenceService without a namespace\n"
    )
    assert elsewhere.code == ExitCode.NOT_FOUND
    assert elsewhere.err == (
        f"Error: InferenceService 'llama' not found (HTTP 404 NOT_FOUND, correlation id "
        f"{correlation_id(api)})\n"
    )
    assert api.log() == [f"GET {PATH}", f"GET {PATH}?namespace=other"]


def test_describe_shows_the_state_of_a_service(api: FakeApi, cli: Cli) -> None:
    service = api.add(
        document(labels={"zone": "b"}),
        phase="Ready",
        ready=("True", "Reconciled"),
        message="instance fake-1 is serving at http://127.0.0.1:9000",
        generation=2,
        observed=2,
        version="5",
    )
    service["status"]["endpoints"] = [
        {
            "url": "http://127.0.0.1:9000",
            "ready": True,
            "protocol": "openai-chat",
            "instanceId": "fake-1",
        }
    ]

    result = cli("describe", "inferenceservice", "llama", "-n", "default")

    assert result.out == (
        "Name:             llama\n"
        "Namespace:        default\n"
        "UID:              uid-1\n"
        "Tenant:           acme\n"
        "Labels:           zone=b\n"
        "Annotations:      <none>\n"
        "Resource version: 5\n"
        "Generation:       2 (observed 2)\n"
        "Model:            models/llama\n"
        "Runtime:          vllm 0.6.3\n"
        "Accelerator:      1 x nvidia-gpu, at least 16 GiB each\n"
        "Phase:            Ready\n"
        "Conditions:\n"
        "  TYPE    STATUS   REASON       LAST TRANSITION        MESSAGE\n"
        "  Ready   True     Reconciled   2026-01-01T00:00:00Z   "
        "instance fake-1 is serving at http://127.0.0.1:9000\n"
        "Endpoints:\n"
        "  URL                     READY   PROTOCOL      INSTANCE\n"
        "  http://127.0.0.1:9000   true    openai-chat   fake-1\n"
    )


def test_describe_of_a_new_service_marks_what_is_missing(api: FakeApi, cli: Cli) -> None:
    api.add(document())

    result = cli("describe", "inferenceservice", "llama", "-n", "default")

    assert result.out.splitlines()[-3:] == [
        "Phase:            Pending",
        "Conditions:  <none>",
        "Endpoints:  <none>",
    ]


def test_list_reads_every_page_and_prints_a_table(api: FakeApi, cli: Cli) -> None:
    api.add(document("a"), **READY)
    api.add(document("b"), phase="Scheduling", ready=("False", "AllocationFailed"))
    api.add(document("c", namespace=None))
    api.add(document("d", namespace="other"))

    result = cli("list", "inferenceservices", "--page-size", "2")

    assert (result.code, result.err) == (0, "")
    assert result.out == (
        "NAMESPACE   NAME   PHASE        READY    REASON             MODEL\n"
        "<none>      c      Pending      <none>   <none>             models/llama\n"
        "default     a      Ready        True     Reconciled         models/llama\n"
        "default     b      Scheduling   False    AllocationFailed   models/llama\n"
        "other       d      Pending      <none>   <none>             models/llama\n"
    )
    assert api.log() == [
        f"GET {SERVICES}?limit=2",
        f"GET {SERVICES}?limit=2&pageToken=2",
    ]


def test_list_keeps_the_namespace_filter_on_every_page(api: FakeApi, cli: Cli) -> None:
    for name in ("a", "b", "c"):
        api.add(document(name))
    api.add(document("d", namespace="other"))

    result = cli("list", "inferenceservices", "-n", "default", "--page-size", "2", "-o", "json")

    assert [service["metadata"]["name"] for service in json.loads(result.out)] == ["a", "b", "c"]
    assert api.log() == [
        f"GET {SERVICES}?limit=2&namespace=default",
        f"GET {SERVICES}?limit=2&namespace=default&pageToken=2",
    ]


def test_list_prints_yaml_documents(api: FakeApi, cli: Cli) -> None:
    first = api.add(document("a"))
    second = api.add(document("b"))

    result = cli("list", "inferenceservices", "-o", "yaml")

    assert result.out.startswith("---\n")
    assert list(yaml.safe_load_all(result.out)) == [first, second]


def test_an_empty_list_is_reported_on_standard_error(api: FakeApi, cli: Cli) -> None:
    table = cli("list", "inferenceservices", "-n", "default")
    everywhere = cli("list", "inferenceservices")
    as_json = cli("list", "inferenceservices", "-o", "json")
    as_yaml = cli("list", "inferenceservices", "-o", "yaml")

    assert (table.code, table.out, table.err) == (
        0,
        "",
        "No InferenceService resources found in namespace default.\n",
    )
    assert everywhere.err == "No InferenceService resources found.\n"
    assert (as_json.out, as_json.err) == ("[]\n", "")
    assert (as_yaml.out, as_yaml.err) == ("", "")


def test_delete_reports_whether_the_service_is_gone_or_terminating(api: FakeApi, cli: Cli) -> None:
    api.add(document("a"))
    api.add(document("b"))

    gone = cli("delete", "inferenceservice", "a", "-n", "default")
    api.delete_status = 202
    terminating = cli("delete", "inferenceservice", "b", "-n", "default")

    assert (gone.code, gone.out) == (0, "InferenceService default/a deleted\n")
    assert (terminating.code, terminating.out) == (
        0,
        "InferenceService default/b is terminating: the controller stops its instance and "
        "releases its accelerators, then deletes it\n",
    )
    assert ("default", "a") not in api.services
    assert api.services[("default", "b")]["status"]["phase"] == "Terminating"
    assert api.log() == [
        f"DELETE {SERVICES}/a?namespace=default",
        f"DELETE {SERVICES}/b?namespace=default",
    ]


def test_delete_of_a_missing_service_fails_with_not_found(api: FakeApi, cli: Cli) -> None:
    result = cli("delete", "inferenceservice", "llama")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err.endswith(
        "no --namespace was given, so this looked for an InferenceService without a namespace\n"
    )


def test_wait_returns_once_the_service_is_ready(api: FakeApi, cli: Cli, clock: FakeClock) -> None:
    api.reply("GET", PATH, 200, resource(document(), phase="Scheduling"))
    api.reply(
        "GET", PATH, 200, resource(document(), phase="Deploying", ready=("False", "Deploying"))
    )
    api.add(document(), **READY)

    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert (result.code, result.out, result.err) == (
        0,
        "InferenceService default/llama is Ready\n",
        "",
    )
    assert clock.sleeps == [1.0, 1.0]
    assert len(api.calls) == 3


def test_wait_fails_at_once_when_the_service_failed(
    api: FakeApi, cli: Cli, clock: FakeClock
) -> None:
    api.add(
        document(),
        phase="Failed",
        ready=("False", "WorkflowFailed"),
        message="model download failed\x1b[2J",
        observed=1,
    )

    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert result.code == ExitCode.WAIT_FAILED
    assert result.err == (
        "Error: InferenceService default/llama will not become Ready: phase Failed, "
        "Ready=False (WorkflowFailed: model download failed?[2J)\n"
    )
    assert clock.sleeps == []


def test_wait_fails_when_the_service_is_being_deleted(
    api: FakeApi, cli: Cli, clock: FakeClock
) -> None:
    api.add(document(), phase="Terminating", observed=1)

    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert (result.code, result.err) == (
        ExitCode.WAIT_FAILED,
        "Error: InferenceService default/llama will not become Ready: phase Terminating\n",
    )


@pytest.mark.parametrize(
    ("phase", "ready"), [("Ready", ("True", "Reconciled")), ("Failed", ("False", "WorkflowFailed"))]
)
def test_wait_ignores_the_outcome_of_an_older_spec(
    api: FakeApi, cli: Cli, clock: FakeClock, phase: str, ready: tuple[str, str]
) -> None:
    """spec 更新之后（generation 2），控制器处理它之前的 Ready 或 Failed 都不是结论。"""
    api.reply(
        "GET", PATH, 200, resource(document(), phase=phase, ready=ready, generation=2, observed=1)
    )
    api.add(document(), phase="Ready", ready=("True", "Reconciled"), generation=2, observed=2)

    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert (result.code, result.out) == (0, "InferenceService default/llama is Ready\n")
    assert clock.sleeps == [1.0]


def test_wait_times_out_with_the_last_state_it_saw(
    api: FakeApi, cli: Cli, clock: FakeClock
) -> None:
    api.add(document(), phase="Ready", ready=("True", "Reconciled"), generation=2, observed=1)

    result = cli("wait", "inferenceservice", "llama", "-n", "default", "--timeout", "2.5")

    assert result.code == ExitCode.WAIT_TIMEOUT
    assert result.err == (
        "Error: timed out after 2.5 s waiting for InferenceService default/llama to become "
        "Ready; last seen: phase Ready, Ready=True (Reconciled); the controller has not "
        "processed generation 2 yet (observed 1)\n"
    )
    assert clock.sleeps == [1.0, 1.0, 0.5]
    assert len(api.calls) == 4


def test_wait_rides_out_rate_limiting_and_an_unreachable_control_plane(
    api: FakeApi, cli: Cli, clock: FakeClock
) -> None:
    api.reply("GET", PATH, 429, raw=b"Too Many Requests\n", headers={"Retry-After": "4"})
    api.reply("GET", PATH, 502, raw=b"Bad Gateway\n")
    api.reply("GET", PATH, DROP)
    api.add(document(), **READY)

    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert (result.code, result.out, result.err) == (
        0,
        "InferenceService default/llama is Ready\n",
        "",
    )
    assert clock.sleeps == [4, 1.0, 1.0]


def test_wait_fails_at_once_when_the_api_cannot_be_reached(
    cli: Cli, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = unused_url()
    monkeypatch.setenv(API_URL_ENV, url)

    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert result.code == ExitCode.CONNECTION_FAILED
    assert result.err.startswith(f"Error: cannot reach the API at {url}: ")
    assert clock.sleeps == []


def test_wait_for_a_missing_service_fails_with_not_found(
    api: FakeApi, cli: Cli, clock: FakeClock
) -> None:
    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert result.code == ExitCode.NOT_FOUND
    assert clock.sleeps == []


def test_wait_for_delete_reports_the_same_result_whether_or_not_it_saw_the_service(
    api: FakeApi, cli: Cli, clock: FakeClock
) -> None:
    api.reply("GET", PATH, 200, resource(document(), phase="Terminating"))

    observed = cli("wait", "inferenceservice", "llama", "-n", "default", "--for", "delete")
    already_gone = cli("wait", "inferenceservice", "llama", "-n", "default", "--for", "delete")

    deleted = Result(ExitCode.OK, "InferenceService default/llama deleted\n", "")
    assert (observed, already_gone) == (deleted, deleted)
    assert api.log() == [f"GET {PATH}?namespace=default"] * 3
    assert clock.sleeps == [1.0]


def test_wait_for_delete_times_out(api: FakeApi, cli: Cli, clock: FakeClock) -> None:
    api.add(document(), phase="Terminating")

    result = cli(
        "wait", "inferenceservice", "llama", "-n", "default", "--for", "delete", "--timeout", "0"
    )

    assert (result.code, result.err) == (
        ExitCode.WAIT_TIMEOUT,
        "Error: timed out after 0 s waiting for InferenceService default/llama to be deleted; "
        "last seen: phase Terminating\n",
    )


def test_an_interrupted_wait_exits_with_130(
    api: FakeApi, cli: Cli, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    api.add(document())
    monkeypatch.setattr(clock, "sleep", interrupt)

    result = cli("wait", "inferenceservice", "llama", "-n", "default")

    assert (result.code, result.out, result.err) == (ExitCode.INTERRUPTED, "", "")


def test_the_api_key_from_a_file_is_sent_as_a_bearer_token_and_never_printed(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    api.reply("GET", PATH, 401, error_body("UNAUTHENTICATED", "invalid credentials"))

    result = cli(
        "get", "inferenceservice", "llama", "-n", "default", "--api-key-file", str(key_file)
    )

    assert (result.code, result.out, result.err) == (
        ExitCode.AUTH,
        "",
        "Error: invalid credentials (HTTP 401 UNAUTHENTICATED, correlation id cid-server)\n",
    )
    assert api.calls[0].headers["authorization"] == f"Bearer {KEY}"


def test_the_key_file_takes_precedence_over_the_environment(
    api: FakeApi, cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text(KEY, encoding="utf-8")
    monkeypatch.setenv(API_KEY_ENV, "fmk_from_the_environment")

    cli("list", "inferenceservices")
    cli("list", "inferenceservices", "--api-key-file", str(key_file))

    assert [call.headers["authorization"] for call in api.calls] == [
        "Bearer fmk_from_the_environment",
        f"Bearer {KEY}",
    ]


def test_a_401_without_a_key_says_how_to_configure_one(api: FakeApi, cli: Cli) -> None:
    api.reply("GET", SERVICES, 401, error_body("UNAUTHENTICATED", "missing bearer credentials"))

    result = cli("list", "inferenceservices")

    assert "authorization" not in api.calls[0].headers
    assert (result.code, result.err) == (
        ExitCode.AUTH,
        "Error: missing bearer credentials (HTTP 401 UNAUTHENTICATED, correlation id "
        "cid-server)\n"
        "no API key is configured: set FORGEMESH_API_KEY or pass --api-key-file (a control "
        "plane in dev mode needs --tenant instead)\n",
    )


def test_a_key_file_that_does_not_hold_a_key_is_not_echoed(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    key_file = tmp_path / "api-key"
    key_file.write_text("password: hunter2\n", encoding="utf-8")

    result = cli("list", "inferenceservices", "--api-key-file", str(key_file))

    assert result.code == ExitCode.USAGE
    assert "hunter2" not in result.err
    assert api.calls == []


def test_common_options_can_be_given_before_or_after_the_command(
    api: FakeApi, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.add(document())
    monkeypatch.setenv(API_URL_ENV, unused_url())

    before = cli("--api-url", api.url, "-n", "default", "get", "inferenceservice", "llama")
    after = cli(
        "-n", "other", "get", "inferenceservice", "llama", "-n", "default", "--api-url", api.url
    )

    assert (before.code, after.code) == (0, 0)
    assert api.log() == [f"GET {PATH}?namespace=default"] * 2


def test_the_requests_of_one_run_share_a_correlation_id(
    api: FakeApi, cli: Cli, tmp_path: Path
) -> None:
    path = write_yaml(tmp_path / "svc.yaml", document())

    cli("apply", "-f", path)
    cli("apply", "-f", path)

    ids = [call.headers["x-correlation-id"] for call in api.calls]
    assert ids[0] == ids[1] != ids[2] == ids[3]
    assert all(re.fullmatch(r"[0-9a-f]{32}", value) for value in ids)


@pytest.mark.parametrize(
    "args",
    [
        ["get", "workflow", "llama"],
        ["get", "inferenceservice"],
        ["apply"],
        ["list", "inferenceservices", "--page-size", "0"],
        ["list", "inferenceservices", "--page-size", "1001"],
        ["list", "inferenceservices", "--page-size", "many"],
        ["list", "inferenceservices", "-o", "wide"],
        ["wait", "inferenceservice", "llama", "--timeout", "-1"],
        ["wait", "inferenceservice", "llama", "--timeout", "nan"],
        ["wait", "inferenceservice", "llama", "--timeout", "inf"],
        ["wait", "inferenceservice", "llama", "--for", "deleted"],
    ],
)
def test_invalid_arguments_exit_with_2(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(args)

    assert raised.value.code == ExitCode.USAGE
    assert "usage: forgemesh" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["--tenant", "team a", "list", "inferenceservices"],
            "--tenant must be a non-empty string of visible ASCII characters",
        ),
        (
            ["list", "inferenceservices", "-n", ""],
            "--namespace must not be empty; omit it for resources without a namespace",
        ),
        (
            ["list", "inferenceservices", "--api-url", "ftp://127.0.0.1"],
            "--api-url must be an http or https URL such as http://127.0.0.1:8081",
        ),
    ],
)
def test_invalid_options_are_reported_before_any_request(
    api: FakeApi, cli: Cli, args: list[str], message: str
) -> None:
    result = cli(*args)

    assert (result.code, result.err) == (ExitCode.USAGE, f"Error: {message}\n")
    assert api.calls == []


def test_the_help_lists_every_exit_code() -> None:
    text = build_parser().format_help()

    listed = re.findall(r"^  (\d+) {2,}\S", text.partition("exit codes:")[2], re.MULTILINE)

    assert [int(code) for code in listed] == [code.value for code in ExitCode]
