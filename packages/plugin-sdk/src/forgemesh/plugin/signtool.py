"""插件签名工具：``python -m forgemesh.plugin.signtool {keygen,sign,verify}``。

* ``keygen --private-key author.key``：生成 Ed25519 私钥（文件权限 0600）与公钥
  ``author.key.pub``，已有文件不会被覆盖；输出 key id 以及可追加到插件策略文件
  ``trustedKeys`` 数组的公钥条目（格式见 :mod:`forgemesh.controlplane.plugin_manager`）。
* ``sign --private-key author.key --manifest manifest.yaml --artifact <制品>``：写出签名
  文档，默认 ``<制品>.sig``。
* ``verify --public-key author.key.pub --manifest manifest.yaml --artifact <制品>``：用给定
  公钥校验签名文档（默认 ``<制品>.sig``），失败时返回 1。

私钥只应由插件作者保管：不要提交到代码仓库，也不要放进插件目录或制品。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from forgemesh.plugin.manifest import PluginManifest
from forgemesh.plugin.signing import (
    PluginSignature,
    PluginSigner,
    SigningError,
    generate_private_key,
    load_public_key,
    load_trusted_keys,
    public_key_id,
    public_key_pem,
)
from pydantic import ValidationError


class _ToolError(Exception):
    """输入文件缺失或非法（退出码 1）。"""


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _ToolError(f"cannot read {path}: {exc}") from exc


def _load_manifest(path: Path) -> PluginManifest:
    try:
        raw = yaml.safe_load(_read_bytes(path))
        return PluginManifest.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise _ToolError(f"invalid manifest {path}: {exc}") from exc


def _load_signature(path: Path) -> PluginSignature:
    try:
        return PluginSignature.model_validate_json(_read_bytes(path))
    except ValidationError as exc:
        raise _ToolError(f"invalid signature document {path}: {exc}") from exc


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _keygen(args: argparse.Namespace) -> int:
    private_path = Path(args.private_key)
    public_path = Path(args.public_key) if args.public_key else Path(f"{private_path}.pub")
    for path in (private_path, public_path):
        if path.exists():
            print(f"refusing to overwrite existing file {path}", file=sys.stderr)
            return 2
    private_pem = generate_private_key()
    public_pem = public_key_pem(private_pem)
    key_id = public_key_id(load_public_key(public_pem))
    _write_private(private_path, private_pem)
    public_path.write_bytes(public_pem)
    print(f"Private key written to {private_path} (keep it secret; never commit or ship it).")
    print(f"Public key written to {public_path}; key id {key_id}.")
    print('Trusted key entry (append it to the "trustedKeys" array of the plugin policy file):')
    print(json.dumps(public_pem.decode("ascii")))
    return 0


def _sign(args: argparse.Namespace) -> int:
    artifact_path = Path(args.artifact)
    output_path = Path(args.output) if args.output else Path(f"{artifact_path}.sig")
    signature = PluginSigner().sign(
        _read_bytes(artifact_path),
        _load_manifest(Path(args.manifest)),
        _read_bytes(Path(args.private_key)),
    )
    output_path.write_text(signature.model_dump_json(by_alias=True) + "\n", encoding="utf-8")
    print(f"Signature written to {output_path} (key id {signature.key_id}).")
    return 0


def _verify(args: argparse.Namespace) -> int:
    artifact_path = Path(args.artifact)
    signature_path = Path(args.signature) if args.signature else Path(f"{artifact_path}.sig")
    trusted = load_trusted_keys([_read_bytes(Path(args.public_key))])
    try:
        key_id = PluginSigner().verify(
            _read_bytes(artifact_path),
            _load_manifest(Path(args.manifest)),
            _load_signature(signature_path),
            trusted,
        )
    except SigningError as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {artifact_path} is signed by key {key_id}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口：成功返回 0，校验失败或输入非法返回 1，参数非法返回 2。"""
    parser = argparse.ArgumentParser(
        prog="python -m forgemesh.plugin.signtool",
        description="Generate signing keys, sign plugin artifacts and verify signatures.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser("keygen", help="generate an Ed25519 key pair")
    keygen.add_argument("--private-key", required=True, help="private key output path")
    keygen.add_argument("--public-key", help="public key output path (default: <private>.pub)")
    keygen.set_defaults(handler=_keygen)

    sign = commands.add_parser("sign", help="sign an artifact and its manifest")
    sign.add_argument("--private-key", required=True, help="Ed25519 private key (PEM)")
    sign.add_argument("--manifest", required=True, help="manifest.yaml of the artifact")
    sign.add_argument("--artifact", required=True, help="artifact file, e.g. the packed tar.gz")
    sign.add_argument("--output", help="signature output path (default: <artifact>.sig)")
    sign.set_defaults(handler=_sign)

    verify = commands.add_parser("verify", help="verify an artifact signature")
    verify.add_argument("--public-key", required=True, help="trusted Ed25519 public key (PEM)")
    verify.add_argument("--manifest", required=True, help="manifest.yaml of the artifact")
    verify.add_argument("--artifact", required=True, help="artifact file")
    verify.add_argument("--signature", help="signature document (default: <artifact>.sig)")
    verify.set_defaults(handler=_verify)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (_ToolError, SigningError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
