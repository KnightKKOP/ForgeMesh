"""``python -m forgemesh.plugin.signtool`` 命令行工具测试。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from forgemesh.plugin.signing import load_trusted_keys
from forgemesh.plugin.signtool import main

_MANIFEST = """\
apiVersion: forgemesh.io/v1alpha1
kind: PluginManifest
metadata:
  id: forgemesh.example.plugin
  version: 1.0.0
spec:
  category: policy
  permissions:
    - resource.read
"""


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    (tmp_path / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
    (tmp_path / "plugin.tar.gz").write_bytes(b"artifact bytes")
    return tmp_path


def _keygen(directory: Path, name: str = "author.key") -> Path:
    assert main(["keygen", "--private-key", str(directory / name)]) == 0
    return directory / name


def _sign_args(plugin: Path, key: Path) -> list[str]:
    return [
        "sign",
        "--private-key",
        str(key),
        "--manifest",
        str(plugin / "manifest.yaml"),
        "--artifact",
        str(plugin / "plugin.tar.gz"),
    ]


def _verify_args(plugin: Path, public_key: Path) -> list[str]:
    return [
        "verify",
        "--public-key",
        str(public_key),
        "--manifest",
        str(plugin / "manifest.yaml"),
        "--artifact",
        str(plugin / "plugin.tar.gz"),
    ]


@pytest.mark.unit
def test_keygen_writes_private_key_owner_only_and_prints_trusted_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = _keygen(tmp_path)

    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    public_pem = (tmp_path / "author.key.pub").read_text(encoding="ascii")
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert entry == public_pem
    assert len(load_trusted_keys([entry])) == 1


@pytest.mark.unit
def test_keygen_refuses_to_overwrite(tmp_path: Path) -> None:
    key = _keygen(tmp_path)
    original = key.read_bytes()

    assert main(["keygen", "--private-key", str(key)]) == 2
    assert key.read_bytes() == original


@pytest.mark.unit
def test_sign_then_verify(plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = _keygen(plugin)

    assert main(_sign_args(plugin, key)) == 0
    document = json.loads((plugin / "plugin.tar.gz.sig").read_text(encoding="utf-8"))
    assert document["algorithm"] == "ed25519"
    assert main(_verify_args(plugin, plugin / "author.key.pub")) == 0
    assert "is signed by key" in capsys.readouterr().out


@pytest.mark.unit
def test_verify_rejects_tampered_artifact(plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = _keygen(plugin)
    assert main(_sign_args(plugin, key)) == 0
    (plugin / "plugin.tar.gz").write_bytes(b"tampered bytes")

    assert main(_verify_args(plugin, plugin / "author.key.pub")) == 1
    assert "artifact digest mismatch" in capsys.readouterr().err


@pytest.mark.unit
def test_verify_rejects_other_public_key(plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = _keygen(plugin)
    _keygen(plugin, "other.key")
    assert main(_sign_args(plugin, key)) == 0

    assert main(_verify_args(plugin, plugin / "other.key.pub")) == 1
    assert "is not trusted" in capsys.readouterr().err


@pytest.mark.unit
def test_sign_reports_missing_files(plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_sign_args(plugin, plugin / "missing.key")) == 1
    assert "cannot read" in capsys.readouterr().err
