"""插件制品签名与校验（Ed25519，对齐 development-plan 5.7 供应链安全）。

设计要点：

* **非对称签名**：插件作者用 Ed25519 私钥签名，控制平面只保存公钥；能验签的一方
  无法伪造签名。
* **签名对象**：签名覆盖「算法标识 + 制品内容摘要 + 清单摘要」，篡改制品内容或清单
  中的任何字段（例如申请的权限）都会使签名失效。
* **不信任调用方给出的摘要**：:meth:`PluginSigner.verify` 总是从制品内容和清单重新
  计算两个摘要，签名文档中记录的摘要只用于给出更明确的拒绝原因。
* **拒绝即报错**：未签名、算法未知、签名公钥不受信任、摘要不符、签名不符，均抛出
  :class:`SigningError`，不静默放行。

签名是一份独立的 JSON 文档（:class:`PluginSignature`），随制品分发；生成密钥、签名与
验签的命令行工具见 :mod:`forgemesh.plugin.signtool`。

``SigningError`` 复用 :class:`forgemesh.core.errors.ForgeMeshError` 统一异常体系；
若核心包未就绪，则回退到本地兜底基类。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable, Mapping

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from forgemesh.plugin.manifest import PluginManifest
from pydantic import BaseModel, ConfigDict, Field

try:  # 优先复用核心包统一异常体系
    from forgemesh.core.errors import ForgeMeshError
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class ForgeMeshError(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的领域异常基类。"""

        def __init__(self, message: str = "", details: dict | None = None) -> None:
            super().__init__(message)
            self.message = message
            self.details = details


#: 唯一支持的签名算法。
SIGNATURE_ALGORITHM = "ed25519"

#: 签名载荷的域分隔前缀：同一把密钥对其他格式数据的签名不能被当作插件签名。
_PAYLOAD_DOMAIN = "forgemesh.io/plugin-signature/v1"

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


class SigningError(ForgeMeshError):
    """签名、验签或密钥加载失败。"""


class PluginSignature(BaseModel):
    """插件制品的签名文档（JSON，随制品分发）。

    字段只按 JSON 名（``keyId`` 等）解析。

    Attributes:
        algorithm: 签名算法，目前只支持 ``ed25519``。
        key_id: 签名公钥的指纹（见 :func:`public_key_id`），验签时据此选择受信任公钥。
        artifact_digest: 签名时的制品内容摘要（``sha256:<hex>``）。
        manifest_digest: 签名时的清单摘要（见 :meth:`PluginSigner.compute_manifest_digest`）。
        signature: 对签名载荷的 Ed25519 签名，标准 base64 编码。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: str
    key_id: str = Field(alias="keyId", min_length=1)
    artifact_digest: str = Field(alias="artifactDigest", pattern=_DIGEST_PATTERN)
    manifest_digest: str = Field(alias="manifestDigest", pattern=_DIGEST_PATTERN)
    signature: str = Field(min_length=1)


def generate_private_key() -> bytes:
    """生成新的 Ed25519 私钥，返回未加密的 PKCS#8 PEM；私钥只应由插件作者保管。"""
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    """加载未加密的 PEM 私钥。

    Raises:
        SigningError: 内容不是合法的 PEM 私钥，或不是 Ed25519 密钥。
    """
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise SigningError(f"invalid private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("private key is not an Ed25519 key")
    return key


def load_public_key(pem: bytes | str) -> Ed25519PublicKey:
    """加载 PEM（SubjectPublicKeyInfo）公钥。

    Raises:
        SigningError: 内容不是合法的 PEM 公钥，或不是 Ed25519 密钥。
    """
    data = pem.encode("utf-8") if isinstance(pem, str) else pem
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise SigningError(f"invalid public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SigningError("public key is not an Ed25519 key")
    return key


def public_key_pem(private_key_pem: bytes) -> bytes:
    """返回私钥对应的公钥 PEM（配置为受信任公钥的就是它）。"""
    return (
        load_private_key(private_key_pem)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def public_key_id(key: Ed25519PublicKey) -> str:
    """公钥指纹：原始 32 字节公钥的 SHA-256 前 16 个十六进制字符。"""
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def load_trusted_keys(pems: Iterable[bytes | str]) -> dict[str, Ed25519PublicKey]:
    """把受信任公钥（PEM）整理为 ``key_id → 公钥``。

    Raises:
        SigningError: 某个公钥非法，或同一公钥出现多次。
    """
    keys: dict[str, Ed25519PublicKey] = {}
    for pem in pems:
        key = load_public_key(pem)
        key_id = public_key_id(key)
        if key_id in keys:
            raise SigningError(f"duplicate trusted key {key_id!r}")
        keys[key_id] = key
    return keys


def _signing_payload(algorithm: str, artifact_digest: str, manifest_digest: str) -> bytes:
    return "\n".join((_PAYLOAD_DOMAIN, algorithm, artifact_digest, manifest_digest)).encode("utf-8")


class PluginSigner:
    """插件制品签名器（Ed25519）。"""

    ALGORITHM = SIGNATURE_ALGORITHM

    @staticmethod
    def compute_digest(content: bytes) -> str:
        """计算制品内容的 SHA-256 摘要，形如 ``sha256:<hex>``。"""
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    @staticmethod
    def compute_manifest_digest(manifest: PluginManifest) -> str:
        """计算清单摘要：清单规范 JSON 形式（键排序、省略取默认值的字段）的 SHA-256。

        省略默认值使同一份清单显式写出默认值（如 ``permissions: []``）与省略时摘要相同，
        后续版本为清单增加带默认值的字段也不会使既有签名失效。
        """
        canonical = json.dumps(
            manifest.model_dump(mode="json", exclude_defaults=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return PluginSigner.compute_digest(canonical.encode("utf-8"))

    def sign(
        self, content: bytes, manifest: PluginManifest, private_key_pem: bytes
    ) -> PluginSignature:
        """对制品内容与清单签名。

        Args:
            content: 制品内容字节（如 :meth:`forgemesh.plugin.sdk.PluginSdk.pack` 产出的
                tar.gz）。
            manifest: 制品对应的插件清单。
            private_key_pem: 插件作者的 Ed25519 私钥（PEM）。

        Returns:
            签名文档。

        Raises:
            SigningError: 私钥非法。
        """
        key = load_private_key(private_key_pem)
        artifact_digest = self.compute_digest(content)
        manifest_digest = self.compute_manifest_digest(manifest)
        signature = key.sign(_signing_payload(self.ALGORITHM, artifact_digest, manifest_digest))
        return PluginSignature(
            algorithm=self.ALGORITHM,
            keyId=public_key_id(key.public_key()),
            artifactDigest=artifact_digest,
            manifestDigest=manifest_digest,
            signature=base64.b64encode(signature).decode("ascii"),
        )

    def verify(
        self,
        content: bytes,
        manifest: PluginManifest,
        signature: PluginSignature | None,
        trusted_keys: Mapping[str, Ed25519PublicKey],
    ) -> str:
        """校验制品签名，通过时返回签名公钥的 key id。

        依次拒绝：未签名、算法不是 ``ed25519``、签名公钥不在 ``trusted_keys`` 中、
        重新计算的制品摘要或清单摘要与签名文档不符、签名不符。签名总是针对重新计算的
        摘要校验，签名文档中记录的摘要不参与判定。

        Args:
            content: 制品内容字节（从制品存储读取，而不是调用方给出的摘要）。
            manifest: 待注册的插件清单。
            signature: 签名文档；``None`` 表示未签名。
            trusted_keys: 受信任公钥（见 :func:`load_trusted_keys`）。

        Raises:
            SigningError: 任一校验失败。
        """
        if signature is None:
            raise SigningError("unsigned plugin rejected")
        if signature.algorithm != self.ALGORITHM:
            raise SigningError(f"unsupported signature algorithm: {signature.algorithm!r}")
        key = trusted_keys.get(signature.key_id)
        if key is None:
            raise SigningError(f"signing key {signature.key_id!r} is not trusted")
        artifact_digest = self.compute_digest(content)
        if artifact_digest != signature.artifact_digest:
            raise SigningError("artifact digest mismatch")
        manifest_digest = self.compute_manifest_digest(manifest)
        if manifest_digest != signature.manifest_digest:
            raise SigningError("manifest digest mismatch")
        payload = _signing_payload(self.ALGORITHM, artifact_digest, manifest_digest)
        try:
            key.verify(base64.b64decode(signature.signature, validate=True), payload)
        except (ValueError, InvalidSignature) as exc:
            raise SigningError("signature verification failed") from exc
        return signature.key_id


__all__ = [
    "SIGNATURE_ALGORITHM",
    "PluginSignature",
    "PluginSigner",
    "SigningError",
    "generate_private_key",
    "load_private_key",
    "load_public_key",
    "load_trusted_keys",
    "public_key_id",
    "public_key_pem",
]
