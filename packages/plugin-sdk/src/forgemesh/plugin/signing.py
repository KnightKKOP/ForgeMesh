"""插件制品签名与摘要校验（对齐 development-plan 5.7 供应链安全）。

设计要点：

* **摘要**：制品内容用 SHA-256 计算摘要（``sha256:<hex>``），供完整性校验。
* **签名**：使用 HMAC-SHA256 对摘要签名，对称密钥模型（``private_key`` 与
  ``public_key`` 为同一密钥）。
* **三重拒绝**：未签名、摘要不符、签名不符分别抛出 :class:`SigningError`，
  拒绝原因明确可审计，不静默放行。

``SigningError`` 复用 :class:`forgemesh.core.errors.ForgeMeshError` 统一异常体系；
若核心包未就绪，则回退到本地兜底基类。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from pydantic import BaseModel

try:  # 优先复用核心包统一异常体系
    from forgemesh.core.errors import ForgeMeshError
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class ForgeMeshError(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的领域异常基类。"""

        def __init__(self, message: str = "", details: dict | None = None) -> None:
            super().__init__(message)
            self.message = message
            self.details = details


class SigningError(ForgeMeshError):
    """签名/摘要校验失败。"""


class PluginSignature(BaseModel):
    """插件签名信息。

    Attributes:
        algorithm: 签名算法标识（如 ``HMAC-SHA256``）。
        signature: 签名原始字节。
        signer: 签名者标识（密钥指纹）。
        signed_at: 签名时间戳（Unix epoch 秒）。
    """

    algorithm: str
    signature: bytes
    signer: str
    signed_at: float


class PluginArtifact(BaseModel):
    """插件制品。

    Attributes:
        name: 制品名称。
        version: 制品语义版本。
        digest: 制品内容摘要（``sha256:<hex>``）。
        signature: 制品签名；未签名时为 ``None``。
        content: 制品内容字节；校验摘要时需要，可为 ``None``。
    """

    name: str
    version: str
    digest: str
    signature: PluginSignature | None = None
    content: bytes | None = None


class PluginSigner:
    """插件制品签名器（HMAC-SHA256）。

    对制品摘要做 HMAC-SHA256 签名；验证时依次校验「是否签名 → 摘要是否一致 →
    签名是否匹配」，任一环节失败均抛出 :class:`SigningError`。
    """

    ALGORITHM = "HMAC-SHA256"

    @staticmethod
    def compute_digest(content: bytes) -> str:
        """计算制品内容的 SHA-256 摘要。

        Args:
            content: 制品内容字节。

        Returns:
            形如 ``sha256:<hex>`` 的摘要字符串。
        """
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def sign(self, artifact: PluginArtifact, private_key: bytes) -> PluginSignature:
        """对制品摘要签名。

        签名对象为 ``artifact.digest``（而非原始内容），因此调用方需先用
        :meth:`compute_digest` 计算并填入 ``artifact.digest``。

        Args:
            artifact: 待签名制品。
            private_key: HMAC 密钥字节。

        Returns:
            生成的 :class:`PluginSignature`。
        """
        sig = hmac.new(private_key, artifact.digest.encode("utf-8"), hashlib.sha256).digest()
        signer = hashlib.sha256(private_key).hexdigest()[:16]
        return PluginSignature(
            algorithm=self.ALGORITHM,
            signature=sig,
            signer=signer,
            signed_at=time.time(),
        )

    def verify(self, artifact: PluginArtifact, public_key: bytes) -> bool:
        """验证制品签名。

        依次执行三重校验，任一失败抛出 :class:`SigningError`：

        1. **未签名拒绝**：``artifact.signature`` 为 ``None``。
        2. **摘要不符拒绝**：``artifact.content`` 非空时重算摘要并与
           ``artifact.digest`` 比对。
        3. **签名不符拒绝**：用 ``public_key`` 重算 HMAC 并与签名比对。

        Args:
            artifact: 待验证制品。
            public_key: HMAC 密钥字节（与签名时相同）。

        Returns:
            验证通过返回 ``True``。

        Raises:
            SigningError: 未签名 / 摘要不符 / 签名不符。
        """
        if artifact.signature is None:
            raise SigningError("unsigned plugin rejected")

        # 摘要校验：仅当制品携带内容时重算并比对。
        if artifact.content is not None:
            computed = self.compute_digest(artifact.content)
            if computed != artifact.digest:
                raise SigningError("digest mismatch")

        # 签名校验：对摘要做 HMAC 并常量时间比对。
        expected = hmac.new(
            public_key, artifact.digest.encode("utf-8"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, artifact.signature.signature):
            raise SigningError("signature verification failed")

        return True


__all__ = [
    "PluginSignature",
    "PluginArtifact",
    "SigningError",
    "PluginSigner",
]
