"""模型准备：拉取模型制品并校验摘要（对齐 development-plan 5.3 / ADR-011）。

模型与制品以引用 + 摘要方式处理，不内嵌明文。本模块负责：

* 从给定 URI 拉取模型制品元信息（``fetch``）；
* 校验制品摘要是否与预期一致（``verify_digest``），不符则拒绝；
* 完整准备流程（``prepare`` = fetch + verify）。

架构不变量：

* **版本固定**（ADR-011）：制品携带 ``version`` 与 ``digest``，部署侧只接受
  摘要匹配的制品，避免隐式漂移。
* **摘要不符拒绝**：``verify_digest`` 在摘要不匹配时抛 ``ValueError``，
  不返回 ``False``，调用方无需二次判断。
* **测试友好**：拉取函数可注入，无真实模型仓库时用 mock 即可测试。

基于 Pydantic v2。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

#: 模型拉取函数类型：给定 URI，返回制品元信息。
ModelFetcher = Callable[[str], Awaitable["ModelArtifact"]]


class ModelArtifact(BaseModel):
    """模型制品信息。

    Attributes:
        name: 制品名称（如 ``llama-3-8b-instruct``）。
        version: 制品版本（如 ``1.0.0``）。
        uri: 制品来源 URI（如 ``hf://meta-llama/Llama-3-8B-Instruct``）。
        digest: 制品摘要（如 ``sha256:abcd...``），拉取后由仓库返回。
        size_bytes: 制品体积（字节），必须 ``>= 0``。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = ""
    uri: str
    digest: str = ""
    size_bytes: int = Field(default=0, ge=0)


async def _default_fetch(uri: str) -> ModelArtifact:
    """默认模型拉取：实际环境应对接模型仓库客户端。

    未注入 fetcher 时抛 ``NotImplementedError``，提示调用方注入 mock 或
    配置真实拉取实现。
    """
    raise NotImplementedError(
        "default model fetcher is not configured; inject a fetcher for production use",
    )


class ModelPreparer:
    """模型准备器：拉取制品并校验摘要。

    Args:
        fetcher: 模型拉取函数（可注入 mock）；默认抛 ``NotImplementedError``。
    """

    def __init__(self, fetcher: ModelFetcher | None = None) -> None:
        self._fetch: ModelFetcher = fetcher or _default_fetch

    async def fetch(self, uri: str) -> ModelArtifact:
        """拉取模型制品元信息。

        Args:
            uri: 模型制品来源 URI。

        Returns:
            拉取到的 :class:`ModelArtifact`。
        """
        return await self._fetch(uri)

    async def verify_digest(self, artifact: ModelArtifact, expected_digest: str) -> bool:
        """校验制品摘要是否与预期一致。

        摘要匹配返回 ``True``；不匹配抛 ``ValueError``（摘要不符拒绝），
        调用方无需二次判断返回值。

        Args:
            artifact: 待校验的模型制品。
            expected_digest: 预期摘要（如 ``sha256:abcd...``）。

        Returns:
            ``True`` 表示摘要匹配。

        Raises:
            ValueError: 摘要不匹配。
        """
        if artifact.digest != expected_digest:
            raise ValueError(
                f"digest mismatch for {artifact.name}: "
                f"expected {expected_digest!r}, got {artifact.digest!r}",
            )
        return True

    async def prepare(self, uri: str, expected_digest: str) -> ModelArtifact:
        """完整准备流程：拉取制品并校验摘要。

        Args:
            uri: 模型制品来源 URI。
            expected_digest: 预期摘要。

        Returns:
            摘要校验通过的 :class:`ModelArtifact`。

        Raises:
            ValueError: 摘要不匹配。
        """
        artifact = await self.fetch(uri)
        await self.verify_digest(artifact, expected_digest)
        return artifact


__all__ = [
    "ModelArtifact",
    "ModelPreparer",
    "ModelFetcher",
]
