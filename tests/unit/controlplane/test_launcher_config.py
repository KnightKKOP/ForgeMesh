"""启动器配置测试：``FORGEMESH_RUNTIME_LAUNCHER`` 及各启动器的环境变量。

* 未设置或为空时不选择启动器，也不读取各启动器的变量；取值不区分大小写；未知取值、缺少
  加速器清单都拒绝启动；
* ``fake``：监听地址与写进实例地址的主机；
* ``vllm-process``：命令（默认要求本解释器装有 ``vllm``）、地址与时限；清单必须恰好有一个
  节点，设备 id 都能写进 ``CUDA_VISIBLE_DEVICES`` 且不会选中同一块设备；
* ``tgi-process``：命令（默认要求 ``PATH`` 中有 ``text-generation-launcher``）、地址与时限
  读自 ``FORGEMESH_TGI_*``，对清单的要求同 ``vllm-process``；
* 控制器核对实例的间隔 ``FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS``：默认 10 秒，取值
  必须是有限的正数秒。
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from forgemesh.controlplane.deployment import (
    FakeLauncher,
    LauncherConfigError,
    TGIProcessLauncher,
    VLLMProcessLauncher,
    instance_check_interval_from_env,
    launcher_from_env,
)
from forgemesh.controlplane.deployment import config as config_module
from forgemesh.controlplane.deployment.vllm_process import DEFAULT_COMMAND
from forgemesh.controlplane.scheduler import Inventory

GPU_UUID = "GPU-8f0c5a62-3b1e-4a7d-9c2f-0d1e2f3a4b5c"
PYTHON = shlex.quote(sys.executable)


def _inventory(*nodes: tuple[str, tuple[str, ...]]) -> Inventory:
    payload = {
        "nodes": [
            {
                "id": node_id,
                "devices": [
                    {"id": device_id, "type": "nvidia-gpu", "memoryGiB": 80}
                    for device_id in device_ids
                ],
            }
            for node_id, device_ids in nodes
        ]
    }
    return Inventory.model_validate_json(json.dumps(payload))


ONE_NODE = _inventory(("node-a", ("gpu-0", "gpu-1")))
VLLM = {"FORGEMESH_RUNTIME_LAUNCHER": "vllm-process", "FORGEMESH_VLLM_COMMAND": PYTHON}
TGI = {"FORGEMESH_RUNTIME_LAUNCHER": "tgi-process", "FORGEMESH_TGI_COMMAND": PYTHON}


def _refusal(inventory: Inventory | None, env: dict[str, str]) -> str:
    with pytest.raises(LauncherConfigError) as caught:
        launcher_from_env(inventory, env)
    return str(caught.value)


# ---------------------------------------------------------------------- #
# 选择启动器
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "  "])
@pytest.mark.parametrize("inventory", [None, ONE_NODE], ids=["no-inventory", "inventory"])
def test_no_launcher_is_selected_when_the_variable_is_unset_or_blank(
    value: str | None, inventory: Inventory | None
) -> None:
    env = {
        "FORGEMESH_FAKE_LAUNCHER_BIND_HOST": "not-an-address",
        "FORGEMESH_VLLM_COMMAND": "/no/such/vllm",
        "FORGEMESH_TGI_COMMAND": "/no/such/tgi",
    }
    if value is not None:
        env["FORGEMESH_RUNTIME_LAUNCHER"] = value

    assert launcher_from_env(inventory, env) is None


@pytest.mark.unit
@pytest.mark.parametrize("value", ["docker", "vllm", "tgi", "fake,vllm-process"])
def test_an_unknown_launcher_is_refused(value: str) -> None:
    message = _refusal(ONE_NODE, {"FORGEMESH_RUNTIME_LAUNCHER": value})

    assert message == (
        f"unsupported FORGEMESH_RUNTIME_LAUNCHER={value!r}; expected 'fake', 'vllm-process' "
        "or 'tgi-process'"
    )


@pytest.mark.unit
@pytest.mark.parametrize("value", ["fake", "vllm-process", "tgi-process"])
def test_a_launcher_requires_the_accelerator_inventory(value: str) -> None:
    message = _refusal(None, {"FORGEMESH_RUNTIME_LAUNCHER": value})

    assert message == (
        f"FORGEMESH_RUNTIME_LAUNCHER={value} requires FORGEMESH_INVENTORY_FILE: without an "
        "accelerator inventory no inference service can be scheduled"
    )


# ---------------------------------------------------------------------- #
# fake
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_fake_launcher_listens_on_loopback_by_default() -> None:
    env = {"FORGEMESH_RUNTIME_LAUNCHER": " Fake ", "FORGEMESH_VLLM_COMMAND": "/no/such/vllm"}

    launcher = launcher_from_env(ONE_NODE, env)

    assert isinstance(launcher, FakeLauncher)
    assert (launcher.bind_host, launcher.advertise_host) == ("127.0.0.1", "127.0.0.1")


@pytest.mark.unit
def test_the_fake_launcher_can_listen_on_all_addresses_under_an_advertised_host() -> None:
    env = {
        "FORGEMESH_RUNTIME_LAUNCHER": "fake",
        "FORGEMESH_FAKE_LAUNCHER_BIND_HOST": "0.0.0.0",
        "FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST": "control-plane",
    }

    launcher = launcher_from_env(ONE_NODE, env)

    assert isinstance(launcher, FakeLauncher)
    assert (launcher.bind_host, launcher.advertise_host) == ("0.0.0.0", "control-plane")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param(
            {"FORGEMESH_FAKE_LAUNCHER_BIND_HOST": "localhost"},
            "invalid FORGEMESH_FAKE_LAUNCHER_BIND_HOST: 'localhost' is not an IP address",
            id="bind-host-name",
        ),
        pytest.param(
            {"FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST": "http://control-plane"},
            "invalid FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST: 'http://control-plane' is neither "
            "an IP address nor a host name",
            id="advertise-url",
        ),
        pytest.param(
            {"FORGEMESH_FAKE_LAUNCHER_BIND_HOST": "0.0.0.0"},
            "FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST must be set when "
            "FORGEMESH_FAKE_LAUNCHER_BIND_HOST=0.0.0.0 listens on all addresses",
            id="wildcard-without-advertise",
        ),
    ],
)
def test_invalid_fake_launcher_addresses_are_refused(
    settings: dict[str, str], message: str
) -> None:
    assert message in _refusal(ONE_NODE, {"FORGEMESH_RUNTIME_LAUNCHER": "fake", **settings})


# ---------------------------------------------------------------------- #
# vllm-process
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_vllm_launcher_runs_the_installed_vllm_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[str] = []

    def find_spec(name: str) -> object:
        looked_up.append(name)
        return object()

    monkeypatch.setattr(config_module, "find_spec", find_spec)
    env = {"FORGEMESH_RUNTIME_LAUNCHER": "vllm-process", "FORGEMESH_FAKE_LAUNCHER_BIND_HOST": "x"}

    launcher = launcher_from_env(ONE_NODE, env)

    assert isinstance(launcher, VLLMProcessLauncher)
    assert looked_up == ["vllm"]
    assert launcher.command == DEFAULT_COMMAND
    assert launcher.node_id == "node-a"
    assert (launcher.bind_host, launcher.advertise_host) == ("127.0.0.1", "127.0.0.1")
    assert (launcher.ready_timeout, launcher.stop_grace) == (1200.0, 10.0)


@pytest.mark.unit
def test_the_default_vllm_command_requires_vllm_to_be_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "find_spec", lambda name: None)

    message = _refusal(ONE_NODE, {"FORGEMESH_RUNTIME_LAUNCHER": "vllm-process"})

    assert "the vllm package is not installed for this interpreter" in message
    assert "FORGEMESH_VLLM_COMMAND" in message


@pytest.mark.unit
def test_the_vllm_launcher_uses_the_configured_command_addresses_and_timeouts() -> None:
    env = {
        "FORGEMESH_RUNTIME_LAUNCHER": "VLLM-Process",
        "FORGEMESH_VLLM_COMMAND": f"{PYTHON} -m my.vllm --served-model-name 'my model'",
        "FORGEMESH_VLLM_BIND_HOST": "::",
        "FORGEMESH_VLLM_ADVERTISE_HOST": "gpu-host.internal",
        "FORGEMESH_VLLM_READY_TIMEOUT_SECONDS": "90",
        "FORGEMESH_VLLM_STOP_GRACE_SECONDS": " 2.5 ",
    }
    inventory = _inventory(("this-host", ("0", GPU_UUID, f"MIG-{GPU_UUID}/1/0")))

    launcher = launcher_from_env(inventory, env)

    assert isinstance(launcher, VLLMProcessLauncher)
    assert launcher.command == (
        sys.executable,
        "-m",
        "my.vllm",
        "--served-model-name",
        "my model",
    )
    assert launcher.node_id == "this-host"
    assert (launcher.bind_host, launcher.advertise_host) == ("::", "gpu-host.internal")
    assert (launcher.ready_timeout, launcher.stop_grace) == (90.0, 2.5)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param(
            {"FORGEMESH_VLLM_COMMAND": "/no/such/vllm serve"},
            "invalid FORGEMESH_VLLM_COMMAND: '/no/such/vllm' is not an executable file",
            id="missing-executable",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_COMMAND": "''"},
            "invalid FORGEMESH_VLLM_COMMAND: '' is not an executable file",
            id="empty-executable",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_COMMAND": "vllm 'serve"},
            "invalid FORGEMESH_VLLM_COMMAND: No closing quotation",
            id="unbalanced-quote",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_READY_TIMEOUT_SECONDS": "soon"},
            "FORGEMESH_VLLM_READY_TIMEOUT_SECONDS='soon' is not a number of seconds",
            id="ready-timeout-text",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_READY_TIMEOUT_SECONDS": "0"},
            "FORGEMESH_VLLM_READY_TIMEOUT_SECONDS='0' must be a positive number of seconds",
            id="ready-timeout-zero",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_STOP_GRACE_SECONDS": "-1"},
            "FORGEMESH_VLLM_STOP_GRACE_SECONDS='-1' must be a positive number of seconds",
            id="stop-grace-negative",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_STOP_GRACE_SECONDS": "inf"},
            "FORGEMESH_VLLM_STOP_GRACE_SECONDS='inf' must be a positive number of seconds",
            id="stop-grace-infinite",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_BIND_HOST": "0.0.0.0"},
            "FORGEMESH_VLLM_ADVERTISE_HOST must be set when FORGEMESH_VLLM_BIND_HOST=0.0.0.0 "
            "listens on all addresses",
            id="wildcard-without-advertise",
        ),
        pytest.param(
            {"FORGEMESH_VLLM_ADVERTISE_HOST": "::"},
            "invalid FORGEMESH_VLLM_ADVERTISE_HOST: '::' is a wildcard address",
            id="wildcard-advertise",
        ),
    ],
)
def test_invalid_vllm_settings_are_refused(settings: dict[str, str], message: str) -> None:
    assert message in _refusal(ONE_NODE, {**VLLM, **settings})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("inventory", "message"),
    [
        pytest.param(
            _inventory(("node-a", ("gpu-0",)), ("node-b", ("gpu-0",))),
            "FORGEMESH_RUNTIME_LAUNCHER=vllm-process runs instances on the control plane host "
            "only, so FORGEMESH_INVENTORY_FILE must describe exactly one node (this host), not "
            "2: node-a, node-b",
            id="two-nodes",
        ),
        pytest.param(
            _inventory(("node-a", ("gpu-0", "accel-1"))),
            "invalid FORGEMESH_INVENTORY_FILE for vLLM: device id 'accel-1' is neither a CUDA "
            "device index",
            id="device-id",
        ),
        pytest.param(
            _inventory(("node-a", ("0", "gpu-0"))),
            "invalid FORGEMESH_INVENTORY_FILE for vLLM: device ids '0' and 'gpu-0' both select "
            "CUDA device 0",
            id="same-device-twice",
        ),
        pytest.param(
            _inventory(("node-a", ("gpu-1", "gpu-01"))),
            "device ids 'gpu-1' and 'gpu-01' both select CUDA device 1",
            id="leading-zero",
        ),
    ],
)
def test_the_vllm_launcher_needs_an_inventory_of_this_host(
    inventory: Inventory, message: str
) -> None:
    assert message in _refusal(inventory, dict(VLLM))


# ---------------------------------------------------------------------- #
# tgi-process
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_tgi_launcher_runs_text_generation_launcher_from_path_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "text-generation-launcher"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    env = {"FORGEMESH_RUNTIME_LAUNCHER": "tgi-process", "FORGEMESH_VLLM_BIND_HOST": "x"}

    launcher = launcher_from_env(ONE_NODE, env)

    assert isinstance(launcher, TGIProcessLauncher)
    assert launcher.command == ("text-generation-launcher",)
    assert launcher.node_id == "node-a"
    assert (launcher.bind_host, launcher.advertise_host) == ("127.0.0.1", "127.0.0.1")
    assert (launcher.ready_timeout, launcher.stop_grace) == (1200.0, 10.0)


@pytest.mark.unit
def test_the_default_tgi_command_must_be_on_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    message = _refusal(ONE_NODE, {"FORGEMESH_RUNTIME_LAUNCHER": "tgi-process"})

    assert message == (
        "FORGEMESH_RUNTIME_LAUNCHER=tgi-process runs text-generation-launcher by default, but "
        "it is not on PATH; install TGI or point FORGEMESH_TGI_COMMAND to its "
        "text-generation-launcher"
    )


@pytest.mark.unit
def test_the_tgi_launcher_uses_the_configured_command_addresses_and_timeouts() -> None:
    env = {
        "FORGEMESH_RUNTIME_LAUNCHER": "TGI-Process",
        "FORGEMESH_TGI_COMMAND": f"{PYTHON} -m my.tgi --max-input-tokens 2048",
        "FORGEMESH_TGI_BIND_HOST": "0.0.0.0",
        "FORGEMESH_TGI_ADVERTISE_HOST": "gpu-host.internal",
        "FORGEMESH_TGI_READY_TIMEOUT_SECONDS": "600",
        "FORGEMESH_TGI_STOP_GRACE_SECONDS": "30",
        "FORGEMESH_VLLM_BIND_HOST": "not-an-address",
        "FORGEMESH_VLLM_READY_TIMEOUT_SECONDS": "soon",
    }
    inventory = _inventory(("this-host", ("0", GPU_UUID)))

    launcher = launcher_from_env(inventory, env)

    assert isinstance(launcher, TGIProcessLauncher)
    assert launcher.command == (sys.executable, "-m", "my.tgi", "--max-input-tokens", "2048")
    assert launcher.node_id == "this-host"
    assert (launcher.bind_host, launcher.advertise_host) == ("0.0.0.0", "gpu-host.internal")
    assert (launcher.ready_timeout, launcher.stop_grace) == (600.0, 30.0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param(
            {"FORGEMESH_TGI_COMMAND": "/no/such/text-generation-launcher"},
            "invalid FORGEMESH_TGI_COMMAND: '/no/such/text-generation-launcher' is not an "
            "executable file",
            id="missing-executable",
        ),
        pytest.param(
            {"FORGEMESH_TGI_READY_TIMEOUT_SECONDS": "0"},
            "FORGEMESH_TGI_READY_TIMEOUT_SECONDS='0' must be a positive number of seconds",
            id="ready-timeout-zero",
        ),
        pytest.param(
            {"FORGEMESH_TGI_STOP_GRACE_SECONDS": "later"},
            "FORGEMESH_TGI_STOP_GRACE_SECONDS='later' is not a number of seconds",
            id="stop-grace-text",
        ),
        pytest.param(
            {"FORGEMESH_TGI_BIND_HOST": "::"},
            "FORGEMESH_TGI_ADVERTISE_HOST must be set when FORGEMESH_TGI_BIND_HOST=:: listens "
            "on all addresses",
            id="wildcard-without-advertise",
        ),
        pytest.param(
            {"FORGEMESH_TGI_ADVERTISE_HOST": "tgi_host"},
            "invalid FORGEMESH_TGI_ADVERTISE_HOST: 'tgi_host' is neither an IP address nor a "
            "host name",
            id="advertise-host",
        ),
    ],
)
def test_invalid_tgi_settings_are_refused(settings: dict[str, str], message: str) -> None:
    assert message in _refusal(ONE_NODE, {**TGI, **settings})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("inventory", "message"),
    [
        pytest.param(
            _inventory(("node-a", ("gpu-0",)), ("node-b", ("gpu-0",))),
            "FORGEMESH_RUNTIME_LAUNCHER=tgi-process runs instances on the control plane host "
            "only, so FORGEMESH_INVENTORY_FILE must describe exactly one node (this host), not "
            "2: node-a, node-b",
            id="two-nodes",
        ),
        pytest.param(
            _inventory(("node-a", ("gpu-0", "0"))),
            "invalid FORGEMESH_INVENTORY_FILE for TGI: device ids 'gpu-0' and '0' both select "
            "CUDA device 0",
            id="same-device-twice",
        ),
    ],
)
def test_the_tgi_launcher_needs_an_inventory_of_this_host(
    inventory: Inventory, message: str
) -> None:
    assert message in _refusal(inventory, dict(TGI))


# ---------------------------------------------------------------------- #
# 核对实例的间隔
# ---------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "interval"), [(None, 10.0), ("", 10.0), ("  ", 10.0), ("2.5", 2.5), (" 30 ", 30.0)]
)
def test_instances_are_checked_every_ten_seconds_unless_configured(
    value: str | None, interval: float
) -> None:
    env = {} if value is None else {"FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS": value}

    assert instance_check_interval_from_env(env) == interval


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("soon", "is not a number of seconds"),
        ("0", "must be a positive number of seconds"),
        ("-5", "must be a positive number of seconds"),
        ("nan", "must be a positive number of seconds"),
        ("inf", "must be a positive number of seconds"),
    ],
)
def test_an_invalid_instance_check_interval_is_refused(value: str, problem: str) -> None:
    with pytest.raises(LauncherConfigError) as caught:
        instance_check_interval_from_env({"FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS": value})

    assert str(caught.value) == f"FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS={value!r} {problem}"


@pytest.mark.unit
def test_settings_are_read_from_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGEMESH_RUNTIME_LAUNCHER", "fake")
    monkeypatch.setenv("FORGEMESH_FAKE_LAUNCHER_ADVERTISE_HOST", "127.0.0.2")
    monkeypatch.delenv("FORGEMESH_FAKE_LAUNCHER_BIND_HOST", raising=False)
    monkeypatch.setenv("FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS", "3")

    launcher = launcher_from_env(ONE_NODE)

    assert isinstance(launcher, FakeLauncher)
    assert launcher.advertise_host == "127.0.0.2"
    assert instance_check_interval_from_env() == 3.0
