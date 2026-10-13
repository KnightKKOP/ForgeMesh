"""CLI 端到端测试：``forgemesh`` 经真实的 HTTP 连接调用控制平面应用（uvicorn，API key 认证，
一块设备的加速器清单与假启动器），不需要 GPU。

* ``apply`` 创建 → ``wait`` 就绪 → 再次 ``apply`` 不变 → 修改 labels 后 ``configured``；
* 无法调度的第二个服务；``list`` 逐页读取；``get``；
* ``delete``：无法调度的服务直接删除（``204``），就绪的服务先进入 ``Terminating``（``202``），
  ``wait --for delete`` 等它消失；
* API 的错误：没有 key（``401``）、文档的租户与 key 不符（``403``）、文档不合法（``400``）。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import yaml
from forgemesh.cli import commands
from forgemesh.cli.config import API_KEY_ENV, API_URL_ENV
from forgemesh.cli.errors import ExitCode
from forgemesh.cli.main import main
from forgemesh.controlplane import app as app_module
from forgemesh.controlplane.api.dependencies import reset_runtime_components
from forgemesh.controlplane.app import create_app
from forgemesh.controlplane.auth import ApiKeyAuthenticator, ApiKeyEntry, hash_api_key
from forgemesh.controlplane.deployment.config import (
    FAKE_ADVERTISE_HOST_ENV,
    FAKE_BIND_HOST_ENV,
    RUNTIME_LAUNCHER_ENV,
)
from forgemesh.controlplane.scheduler import INVENTORY_FILE_ENV

TENANT = "acme"
KEY = "fmk_e2e-" + "k" * 40
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str


Cli = Callable[..., Result]


def service(name: str, *, count: int = 1, **metadata: Any) -> dict[str, Any]:
    return {
        "apiVersion": "forgemesh.io/v1alpha1",
        "kind": "InferenceService",
        "metadata": {"name": name, "namespace": "default", **metadata},
        "spec": {
            "model": {"reference": "models/llama"},
            "runtime": {"provider": "vllm"},
            "resources": {
                "accelerator": {"type": "nvidia-gpu", "count": count, "minMemoryGiB": 16}
            },
        },
    }


@pytest.fixture
def control_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """在后台线程中以 uvicorn 运行控制平面应用，``FORGEMESH_API_URL`` 指向它。"""
    inventory = tmp_path / "inventory.json"
    gpu = {"id": "gpu-0", "type": "nvidia-gpu", "memoryGiB": 80}
    inventory.write_text(json.dumps({"nodes": [{"id": "node-a", "devices": [gpu]}]}))
    monkeypatch.setenv(INVENTORY_FILE_ENV, str(inventory))
    monkeypatch.setenv(RUNTIME_LAUNCHER_ENV, "fake")
    monkeypatch.delenv(FAKE_BIND_HOST_ENV, raising=False)
    monkeypatch.delenv(FAKE_ADVERTISE_HOST_ENV, raising=False)
    monkeypatch.setattr(app_module, "CONTROLLER_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "WORKFLOW_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(commands, "POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    # 环境中的 http_proxy 等变量不能把发往本机的请求转给代理
    monkeypatch.setenv("no_proxy", "*")
    reset_runtime_components()
    entry = ApiKeyEntry(id="acme-cli", sha256=hash_api_key(KEY), tenantId=TENANT, subject="cli")
    application = create_app(
        db_path=str(tmp_path / "cp.db"), authenticator=ApiKeyAuthenticator([entry])
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while not server.started:
            assert thread.is_alive(), "the control plane failed to start"
            assert time.monotonic() < deadline, "the control plane did not start in time"
            time.sleep(0.01)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        monkeypatch.setenv(API_URL_ENV, url)
        yield url
    finally:
        server.should_exit = True
        thread.join(TIMEOUT_SECONDS)
        listener.close()
        reset_runtime_components()
    assert not thread.is_alive(), "the control plane did not stop"


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> Cli:
    """运行 ``forgemesh``；``key=True`` 时带上 key 文件（``--api-key-file``）。"""
    key_file = tmp_path / "api-key"
    key_file.write_text(KEY + "\n", encoding="utf-8")

    def run(*args: str, key: bool = True) -> Result:
        code = main([*args, "--api-key-file", str(key_file)] if key else list(args))
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return run


def _write(path: Path, *docs: dict[str, Any]) -> str:
    path.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
    return str(path)


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.e2e
def test_a_service_is_applied_deployed_listed_and_deleted_with_the_cli(
    control_plane: str, cli: Cli, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    llama = tmp_path / "llama.yaml"
    timeout = ("--timeout", str(TIMEOUT_SECONDS))

    created = cli("apply", "-f", _write(llama, service("llama")))
    ready = cli("wait", "inferenceservice", "llama", "-n", "default", *timeout)
    unchanged = cli("apply", "-f", str(llama))
    configured = cli("apply", "-f", _write(llama, service("llama", labels={"zone": "b"})))
    unschedulable = cli("apply", "-f", _write(tmp_path / "big.yaml", service("big", count=2)))
    listed = cli("list", "inferenceservices", "--page-size", "1", "-o", "json")
    shown = cli("get", "inferenceservice", "llama", "-n", "default", "-o", "json")
    deleted = cli("delete", "inferenceservice", "big", "-n", "default")
    terminating = cli("delete", "inferenceservice", "llama", "-n", "default")
    gone = cli("wait", "inferenceservice", "llama", "-n", "default", "--for", "delete", *timeout)
    empty = cli("list", "inferenceservices")

    assert created.out == "InferenceService default/llama created\n"
    assert ready.out == "InferenceService default/llama is Ready\n"
    assert unchanged.out == "InferenceService default/llama unchanged\n"
    assert configured.out == "InferenceService default/llama configured\n"
    assert unschedulable.out == "InferenceService default/big created\n"
    assert [item["metadata"]["name"] for item in json.loads(listed.out)] == ["big", "llama"]
    llama_service = json.loads(shown.out)
    assert llama_service["metadata"]["labels"] == {"zone": "b"}
    assert llama_service["metadata"]["tenantId"] == TENANT
    assert llama_service["status"]["phase"] == "Ready"
    assert deleted.out == "InferenceService default/big deleted\n"
    assert terminating.out == (
        "InferenceService default/llama is terminating: the controller stops its instance and "
        "releases its accelerators, then deletes it\n"
    )
    assert gone.out == "InferenceService default/llama deleted\n"
    assert (empty.out, empty.err) == ("", "No InferenceService resources found.\n")
    results = (created, ready, unchanged, configured, unschedulable, listed, shown, deleted)
    assert [result.code for result in (*results, terminating, gone, empty)] == [0] * 11
    assert all(result.err == "" for result in results)
    assert _errors(caplog) == []


@pytest.mark.e2e
def test_api_errors_are_reported_with_their_exit_codes(
    control_plane: str, cli: Cli, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    invalid = service("llama")
    del invalid["spec"]["resources"]

    without_key = cli("list", "inferenceservices", key=False)
    other_tenant = cli("apply", "-f", _write(tmp_path / "a.yaml", service("a", tenantId="other")))
    rejected = cli("apply", "-f", _write(tmp_path / "llama.yaml", invalid))
    missing = cli("get", "inferenceservice", "llama", "-n", "default")

    assert without_key.code == ExitCode.AUTH
    assert without_key.err.startswith("Error: missing bearer credentials (HTTP 401 UNAUTHENTICATED")
    assert without_key.err.endswith(
        "no API key is configured: set FORGEMESH_API_KEY or pass --api-key-file (a control "
        "plane in dev mode needs --tenant instead)\n"
    )
    assert other_tenant.code == ExitCode.AUTH
    assert other_tenant.err.startswith(
        f"Error: InferenceService default/a ({tmp_path / 'a.yaml'}): X-Tenant-Id does not match "
        "the tenant bound to the credentials (HTTP 403 FORBIDDEN"
    )
    assert rejected.code == ExitCode.REJECTED
    assert "(HTTP 400 BAD_REQUEST" in rejected.err
    assert "\n  " in rejected.err
    assert missing.code == ExitCode.NOT_FOUND
    assert KEY not in "".join(r.out + r.err for r in (without_key, other_tenant, rejected, missing))
    assert _errors(caplog) == []
