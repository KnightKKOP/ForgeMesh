"""CLI 命令单元测试。

覆盖 apply/get/list 命令的核心路径：

* apply 临时 YAML 文件 → 返回 0 且输出含 ``Created``；
* get 不存在资源 → 返回 1；
* list 空库 → 返回 0。

以 ``asyncio.run`` 驱动异步命令，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from forgemesh.cli.commands import cmd_apply, cmd_get, cmd_list


def test_apply_yaml_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """写一个临时 YAML → apply → 返回 0。"""
    yaml_path = tmp_path / "svc.yaml"
    yaml_path.write_text(
        yaml.dump(
            {
                "apiVersion": "forgemesh.io/v1alpha1",
                "kind": "InferenceService",
                "metadata": {"name": "svc-1", "namespace": "default"},
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(file=str(yaml_path), tenant="default", namespace="default")
    rc = asyncio.run(cmd_apply(args))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Created" in out
    assert "InferenceService/svc-1" in out


def test_get_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    """get 不存在的资源 → 返回 1。"""
    args = SimpleNamespace(
        kind="InferenceService", name="nope", tenant="default", namespace="default"
    )
    rc = asyncio.run(cmd_get(args))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err


def test_list_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """list 空库 → 返回 0。"""
    args = SimpleNamespace(kind="InferenceService", tenant="default", namespace=None)
    rc = asyncio.run(cmd_list(args))
    assert rc == 0
    out = capsys.readouterr().out
    assert out == ""
