"""ForgeMesh 密钥引用契约。

安全约束（architecture 13 / 14）：**只存引用，不含明文**。资源对象、事件与审计
中出现的机密信息一律以 :class:`SecretRef` 表达，真实值由外部 Secret Provider
（如 Vault / KMS / K8s Secret）在运行时按需解析。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SecretRef(BaseModel):
    """对一条机密的**引用**（不含明文）。

    Attributes:
        provider: 机密后端标识（如 ``vault`` / ``k8s`` / ``env``）。
        name: 机密对象名称（如 secret 路径/名称）。
        key: 机密内的字段键（如 ``token`` / ``password``）。
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    name: str
    key: str

    async def resolve(self) -> str:
        """解析出机密明文。

        默认实现**不支持**解析（阶段 0 仅定义契约），调用方应由具体 Provider
        子类覆盖此方法。绝不将明文写入日志、事件或资源对象。
        """
        raise NotImplementedError(
            "SecretRef.resolve() 必须由具体 Secret Provider 实现；核心契约不持有明文"
        )


__all__ = ["SecretRef"]
