"""服务验收：冒烟推理、延迟与错误率测量。

对齐 ``docs/architecture/development-plan.md`` 5.3。在 vLLM 实例路由注册后，
通过 HTTP 请求验证服务可推理，并测量延迟与错误率是否满足验收阈值。

架构不变量：

* **验收标准可配**：延迟超阈值或错误率超阈值 → ``passed=False``。
* **测试友好**：HTTP 客户端可注入，无真实服务时用 mock 即可测试。

基于 Pydantic v2。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: HTTP 推理请求函数类型：给定 endpoint 与 prompt，返回响应 dict。
#:
#: 响应 dict 约定字段：
#:
#: * ``ok`` (bool)：请求是否成功（HTTP 2xx 且有响应文本）。
#: * ``latency_ms`` (float)：本次请求延迟（毫秒）。
#: * ``text`` (str)：推理输出文本（失败时可为空）。
#: * ``status`` (int)：HTTP 状态码（失败时可为 0）。
HttpClient = Callable[[str, str], Awaitable[dict[str, Any]]]


class VerifyConfig(BaseModel):
    """验收配置。

    Attributes:
        endpoint: 待验收服务端点。
        test_prompt: 冒烟推理使用的测试 prompt。
        max_latency_ms: 平均延迟阈值（毫秒），超过则验收失败。
        max_error_rate: 错误率阈值（0.0–1.0），超过则验收失败。
        timeout_seconds: 单次请求超时（秒）。
        num_requests: 延迟与错误率测量的请求次数。
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: str
    test_prompt: str = "Hello, who are you?"
    max_latency_ms: float = Field(default=2000.0, gt=0)
    max_error_rate: float = Field(default=0.05, ge=0, le=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    num_requests: int = Field(default=10, ge=1)


class VerifyResult(BaseModel):
    """验收结果。

    Attributes:
        passed: 是否通过验收。
        latency_ms: 实测平均延迟（毫秒）。
        error_rate: 实测错误率（0.0–1.0）。
        response: 冒烟推理响应文本。
        details: 诊断详情（冒烟是否通过、阈值等）。
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool = False
    latency_ms: float = 0.0
    error_rate: float = 0.0
    response: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


async def _default_http_client(endpoint: str, prompt: str) -> dict[str, Any]:
    """默认 HTTP 客户端：实际环境应使用 httpx/aiohttp 发起推理请求。

    未注入 client 时抛 ``NotImplementedError``，提示调用方注入 mock 或
    配置真实 HTTP 客户端。
    """
    raise NotImplementedError(
        "default HTTP client is not configured; inject a client for production use",
    )


class ServiceVerifier:
    """服务验收器：冒烟推理、延迟与错误率测量。

    Args:
        http_client: HTTP 推理请求函数（可注入 mock）；
            默认抛 ``NotImplementedError``。
    """

    def __init__(self, http_client: HttpClient | None = None) -> None:
        self._http: HttpClient = http_client or _default_http_client

    async def smoke_test(self, endpoint: str, prompt: str) -> bool:
        """冒烟推理：单次请求验证服务可推理。

        Args:
            endpoint: 服务端点。
            prompt: 测试 prompt。

        Returns:
            ``True`` 表示请求成功（``ok`` 为真）。
        """
        try:
            resp = await self._http(endpoint, prompt)
        except Exception:
            return False
        return bool(resp.get("ok", False))

    async def measure_latency(self, endpoint: str, num_requests: int) -> float:
        """测量平均延迟。

        发送 ``num_requests`` 次请求，返回平均延迟（毫秒）。
        请求异常时该次延迟计为 0。

        Args:
            endpoint: 服务端点。
            num_requests: 请求次数。

        Returns:
            平均延迟（毫秒）；``num_requests`` 为 0 时返回 0。
        """
        if num_requests <= 0:
            return 0.0
        total = 0.0
        for _ in range(num_requests):
            try:
                resp = await self._http(endpoint, "")
            except Exception:
                continue
            total += float(resp.get("latency_ms", 0.0))
        return total / num_requests

    async def measure_error_rate(self, endpoint: str, num_requests: int) -> float:
        """测量错误率。

        发送 ``num_requests`` 次请求，返回失败占比（0.0–1.0）。
        请求异常计为失败。

        Args:
            endpoint: 服务端点。
            num_requests: 请求次数。

        Returns:
            错误率（0.0–1.0）；``num_requests`` 为 0 时返回 0。
        """
        if num_requests <= 0:
            return 0.0
        failures = 0
        for _ in range(num_requests):
            try:
                resp = await self._http(endpoint, "")
            except Exception:
                failures += 1
                continue
            if not bool(resp.get("ok", False)):
                failures += 1
        return failures / num_requests

    async def verify(self, config: VerifyConfig) -> VerifyResult:
        """完整验收：冒烟推理 + 延迟测量 + 错误率测量。

        验收通过条件：冒烟通过 且 平均延迟 ≤ ``max_latency_ms`` 且
        错误率 ≤ ``max_error_rate``。

        Args:
            config: 验收配置。

        Returns:
            :class:`VerifyResult`。
        """
        smoke_ok = await self.smoke_test(config.endpoint, config.test_prompt)
        latency_ms = await self.measure_latency(
            config.endpoint, config.num_requests,
        )
        error_rate = await self.measure_error_rate(
            config.endpoint, config.num_requests,
        )

        # 冒烟响应文本（再发一次以取文本，避免影响上述测量计数）。
        response_text = ""
        if smoke_ok:
            try:
                resp = await self._http(config.endpoint, config.test_prompt)
                response_text = str(resp.get("text", ""))
            except Exception:
                response_text = ""

        latency_ok = latency_ms <= config.max_latency_ms
        error_ok = error_rate <= config.max_error_rate
        passed = smoke_ok and latency_ok and error_ok

        details: dict[str, Any] = {
            "smoke_ok": smoke_ok,
            "latency_ok": latency_ok,
            "error_ok": error_ok,
            "max_latency_ms": config.max_latency_ms,
            "max_error_rate": config.max_error_rate,
            "num_requests": config.num_requests,
        }
        return VerifyResult(
            passed=passed,
            latency_ms=latency_ms,
            error_rate=error_rate,
            response=response_text,
            details=details,
        )


__all__ = [
    "HttpClient",
    "ServiceVerifier",
    "VerifyConfig",
    "VerifyResult",
]
