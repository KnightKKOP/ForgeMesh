"""插件签名与校验单元测试（Ed25519，对齐 development-plan 5.7 供应链安全）。

覆盖：
* sign + verify 成功，返回签名公钥的 key id；
* 未签名、算法不符、公钥不受信任被拒绝；
* 篡改制品内容 / 清单 / 签名被拒绝，签名文档中伪造的摘要不被信任；
* 清单摘要的规范化；
* 密钥加载与受信任公钥整理。
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from forgemesh.plugin.manifest import PluginManifest, PluginMeta, PluginSpec
from forgemesh.plugin.signing import (
    SIGNATURE_ALGORITHM,
    PluginSignature,
    PluginSigner,
    SigningError,
    generate_private_key,
    load_private_key,
    load_public_key,
    load_trusted_keys,
    public_key_id,
    public_key_pem,
)
from pydantic import ValidationError

_SIGNER = PluginSigner()
_PRIVATE_KEY = generate_private_key()
_OTHER_PRIVATE_KEY = generate_private_key()
_TRUSTED = load_trusted_keys([public_key_pem(_PRIVATE_KEY)])
_CONTENT = b"plugin payload"


def _manifest(permissions: list[str] | None = None) -> PluginManifest:
    return PluginManifest(
        metadata=PluginMeta(id="forgemesh.example.plugin", version="1.0.0"),
        spec=PluginSpec(category="policy", permissions=permissions or ["resource.read"]),
    )


def _key_id(private_key: bytes) -> str:
    return public_key_id(load_public_key(public_key_pem(private_key)))


class TestSignAndVerify:
    """签名与验证成功路径。"""

    def test_sign_then_verify_returns_key_id(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)

        assert _SIGNER.verify(_CONTENT, _manifest(), signature, _TRUSTED) == _key_id(_PRIVATE_KEY)

    def test_signature_document_fields(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)

        assert signature.algorithm == SIGNATURE_ALGORITHM == "ed25519"
        assert signature.key_id == _key_id(_PRIVATE_KEY)
        assert signature.artifact_digest == PluginSigner.compute_digest(_CONTENT)
        assert signature.manifest_digest == PluginSigner.compute_manifest_digest(_manifest())
        assert len(base64.b64decode(signature.signature)) == 64

    def test_signature_document_json_round_trip(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)
        document = signature.model_dump_json(by_alias=True)

        assert '"keyId"' in document and '"artifactDigest"' in document
        assert PluginSignature.model_validate_json(document) == signature

    def test_signature_document_rejects_unknown_fields(self) -> None:
        data = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY).model_dump(by_alias=True)
        data["signer"] = "someone"

        with pytest.raises(ValidationError):
            PluginSignature.model_validate(data)


class TestRejected:
    """任一校验失败都抛出 SigningError。"""

    def test_unsigned_rejected(self) -> None:
        with pytest.raises(SigningError, match="unsigned plugin rejected"):
            _SIGNER.verify(_CONTENT, _manifest(), None, _TRUSTED)

    @pytest.mark.parametrize("algorithm", ["HMAC-SHA256", "ED25519", "rsa-pss", ""])
    def test_algorithm_mismatch_rejected(self, algorithm: str) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)
        forged = signature.model_copy(update={"algorithm": algorithm})

        with pytest.raises(SigningError, match="unsupported signature algorithm"):
            _SIGNER.verify(_CONTENT, _manifest(), forged, _TRUSTED)

    def test_untrusted_key_rejected(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _OTHER_PRIVATE_KEY)

        with pytest.raises(SigningError, match="is not trusted"):
            _SIGNER.verify(_CONTENT, _manifest(), signature, _TRUSTED)

    def test_no_trusted_keys_rejects_everything(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)

        with pytest.raises(SigningError, match="is not trusted"):
            _SIGNER.verify(_CONTENT, _manifest(), signature, {})

    def test_tampered_artifact_rejected(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)

        with pytest.raises(SigningError, match="artifact digest mismatch"):
            _SIGNER.verify(b"tampered payload", _manifest(), signature, _TRUSTED)

    def test_forged_digest_in_signature_document_not_trusted(self) -> None:
        """篡改内容后同步改写签名文档中的摘要，签名仍针对重新计算的摘要校验。"""
        tampered = b"tampered payload"
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)
        forged = signature.model_copy(
            update={"artifact_digest": PluginSigner.compute_digest(tampered)}
        )

        with pytest.raises(SigningError, match="signature verification failed"):
            _SIGNER.verify(tampered, _manifest(), forged, _TRUSTED)

    def test_tampered_manifest_rejected(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)
        escalated = _manifest(["resource.read", "platform.admin"])

        with pytest.raises(SigningError, match="manifest digest mismatch"):
            _SIGNER.verify(_CONTENT, escalated, signature, _TRUSTED)

    def test_tampered_signature_bytes_rejected(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)
        flipped = bytes(b ^ 0xFF for b in base64.b64decode(signature.signature))
        forged = signature.model_copy(
            update={"signature": base64.b64encode(flipped).decode("ascii")}
        )

        with pytest.raises(SigningError, match="signature verification failed"):
            _SIGNER.verify(_CONTENT, _manifest(), forged, _TRUSTED)

    @pytest.mark.parametrize("value", ["not base64!", "QUJD", "签名"])
    def test_malformed_signature_rejected(self, value: str) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _PRIVATE_KEY)
        forged = signature.model_copy(update={"signature": value})

        with pytest.raises(SigningError, match="signature verification failed"):
            _SIGNER.verify(_CONTENT, _manifest(), forged, _TRUSTED)

    def test_signature_by_other_key_under_trusted_key_id_rejected(self) -> None:
        signature = _SIGNER.sign(_CONTENT, _manifest(), _OTHER_PRIVATE_KEY)
        forged = signature.model_copy(update={"key_id": _key_id(_PRIVATE_KEY)})

        with pytest.raises(SigningError, match="signature verification failed"):
            _SIGNER.verify(_CONTENT, _manifest(), forged, _TRUSTED)


class TestDigests:
    """制品摘要与清单摘要。"""

    def test_digest_format_and_determinism(self) -> None:
        digest = PluginSigner.compute_digest(b"x")

        assert digest == PluginSigner.compute_digest(b"x")
        assert digest != PluginSigner.compute_digest(b"y")
        assert digest.startswith("sha256:")
        assert len(digest.removeprefix("sha256:")) == 64

    def test_manifest_digest_ignores_explicit_defaults(self) -> None:
        explicit = PluginManifest.model_validate(
            {
                "apiVersion": "forgemesh.io/v1alpha1",
                "kind": "PluginManifest",
                "metadata": {"id": "forgemesh.example.plugin", "version": "1.0.0"},
                "spec": {
                    "category": "policy",
                    "permissions": ["resource.read"],
                    "provides": [],
                    "configuration": {},
                },
            }
        )

        assert PluginSigner.compute_manifest_digest(
            explicit
        ) == PluginSigner.compute_manifest_digest(_manifest())

    def test_manifest_digest_covers_permissions(self) -> None:
        assert PluginSigner.compute_manifest_digest(
            _manifest(["resource.read"])
        ) != PluginSigner.compute_manifest_digest(_manifest(["resource.write"]))


class TestKeys:
    """密钥生成、加载与受信任公钥。"""

    def test_generated_private_key_is_pkcs8_pem(self) -> None:
        assert _PRIVATE_KEY.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert public_key_pem(_PRIVATE_KEY).startswith(b"-----BEGIN PUBLIC KEY-----")

    def test_load_public_key_accepts_str(self) -> None:
        pem = public_key_pem(_PRIVATE_KEY).decode("ascii")

        assert public_key_id(load_public_key(pem)) == _key_id(_PRIVATE_KEY)

    @pytest.mark.parametrize("pem", [b"", b"garbage", b"-----BEGIN PUBLIC KEY-----\nAA==\n"])
    def test_invalid_public_key_rejected(self, pem: bytes) -> None:
        with pytest.raises(SigningError, match="invalid public key"):
            load_public_key(pem)

    def test_invalid_private_key_rejected(self) -> None:
        with pytest.raises(SigningError, match="invalid private key"):
            load_private_key(b"garbage")

    def test_non_ed25519_keys_rejected(self) -> None:
        ec_key = ec.generate_private_key(ec.SECP256R1())
        ec_private = ec_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        ec_public = ec_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

        with pytest.raises(SigningError, match="not an Ed25519 key"):
            load_private_key(ec_private)
        with pytest.raises(SigningError, match="not an Ed25519 key"):
            load_public_key(ec_public)

    def test_trusted_keys_indexed_by_key_id(self) -> None:
        keys = load_trusted_keys([public_key_pem(_PRIVATE_KEY), public_key_pem(_OTHER_PRIVATE_KEY)])

        assert set(keys) == {_key_id(_PRIVATE_KEY), _key_id(_OTHER_PRIVATE_KEY)}

    def test_duplicate_trusted_key_rejected(self) -> None:
        pem = public_key_pem(_PRIVATE_KEY)

        with pytest.raises(SigningError, match="duplicate trusted key"):
            load_trusted_keys([pem, pem])
