"""插件签名与摘要校验单元测试（对齐 development-plan 5.7 供应链安全）。

覆盖：
* sign + verify 成功；
* 未签名拒绝（SigningError）；
* 摘要不符拒绝 / 摘要相符通过；
* 签名不符拒绝；
* compute_digest 一致性。
"""

from __future__ import annotations

import pytest
from forgemesh.plugin.signing import (
    PluginArtifact,
    PluginSignature,
    PluginSigner,
    SigningError,
)

_SIGNER = PluginSigner()
_KEY = b"test-secret-key"


def _make_artifact(
    content: bytes = b"plugin payload",
    digest: str | None = None,
    signature: PluginSignature | None = None,
) -> PluginArtifact:
    """构造测试用制品。"""
    return PluginArtifact(
        name="forgemesh.plugin.example",
        version="1.0.0",
        digest=digest if digest is not None else PluginSigner.compute_digest(content),
        signature=signature,
        content=content,
    )


class TestSignAndVerify:
    """签名与验证成功路径。"""

    def test_sign_then_verify_succeeds(self) -> None:
        artifact = _make_artifact()
        artifact.signature = _SIGNER.sign(artifact, _KEY)

        assert _SIGNER.verify(artifact, _KEY) is True

    def test_signed_artifact_has_algorithm(self) -> None:
        artifact = _make_artifact()
        sig = _SIGNER.sign(artifact, _KEY)

        assert sig.algorithm == "HMAC-SHA256"
        assert isinstance(sig.signature, bytes)
        assert len(sig.signature) > 0
        assert sig.signer  # 非空


class TestUnsignedRejected:
    """未签名制品拒绝。"""

    def test_unsigned_raises(self) -> None:
        artifact = _make_artifact(signature=None)

        with pytest.raises(SigningError, match="unsigned plugin rejected"):
            _SIGNER.verify(artifact, _KEY)


class TestDigestMismatch:
    """摘要校验。"""

    def test_digest_mismatch_raises(self) -> None:
        artifact = _make_artifact(digest="sha256:deadbeef")
        artifact.signature = _SIGNER.sign(artifact, _KEY)

        with pytest.raises(SigningError, match="digest mismatch"):
            _SIGNER.verify(artifact, _KEY)

    def test_digest_match_passes(self) -> None:
        content = b"correct payload"
        artifact = _make_artifact(content=content)
        artifact.signature = _SIGNER.sign(artifact, _KEY)

        assert _SIGNER.verify(artifact, _KEY) is True

    def test_no_content_skips_digest_check(self) -> None:
        """制品不携带内容时跳过摘要重算，仅校验签名。"""
        artifact = _make_artifact(content=b"original")
        artifact.signature = _SIGNER.sign(artifact, _KEY)
        artifact.content = None  # 移除内容，保留正确摘要与签名

        assert _SIGNER.verify(artifact, _KEY) is True


class TestSignatureMismatch:
    """签名不符拒绝。"""

    def test_wrong_key_raises(self) -> None:
        artifact = _make_artifact()
        artifact.signature = _SIGNER.sign(artifact, _KEY)

        with pytest.raises(SigningError, match="signature verification failed"):
            _SIGNER.verify(artifact, b"different-key")

    def test_tampered_signature_raises(self) -> None:
        artifact = _make_artifact()
        sig = _SIGNER.sign(artifact, _KEY)
        # 篡改签名字节
        tampered = PluginSignature(
            algorithm=sig.algorithm,
            signature=bytes(b ^ 0xFF for b in sig.signature),
            signer=sig.signer,
            signed_at=sig.signed_at,
        )
        artifact.signature = tampered

        with pytest.raises(SigningError, match="signature verification failed"):
            _SIGNER.verify(artifact, _KEY)


class TestComputeDigest:
    """compute_digest 一致性。"""

    def test_same_content_same_digest(self) -> None:
        content = b"identical payload"
        d1 = PluginSigner.compute_digest(content)
        d2 = PluginSigner.compute_digest(content)

        assert d1 == d2

    def test_different_content_different_digest(self) -> None:
        d1 = PluginSigner.compute_digest(b"payload-a")
        d2 = PluginSigner.compute_digest(b"payload-b")

        assert d1 != d2

    def test_digest_format(self) -> None:
        digest = PluginSigner.compute_digest(b"x")

        assert digest.startswith("sha256:")
        hex_part = digest.removeprefix("sha256:")
        assert len(hex_part) == 64  # SHA-256 hex 长度
