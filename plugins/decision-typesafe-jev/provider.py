"""TypeSafe Jev 决策 Provider（对齐 development-plan 5.5 / ADR-004）。

接入 TypeSafe AI 的 Jev（System One 结构化决策模型）：把
:class:`DecisionRequest` 的上下文映射为 Jev 的 ``state``，把候选动作集合
映射为 Jev 的 ``questions``（类型化选项），再将 Jev 返回的「类型化、校准
概率」决策映射为 :class:`DecisionCandidate`。

架构不变量：

* **决策 ≠ 授权**（INV-1/INV-3）：仅产出候选，不分配资源、不改写资源终态。
* ``confidence`` 为 Jev 的校准概率，仅供排序/解释，**绝不**作为授权凭证。
* **绝不默认放行**：超时/不可用时抛出异常，交由 :class:`DecisionRouter`
  按序降级；「Jev 不可用」绝不解释为「允许执行默认操作」。

外部依赖（TypeSafe 云端 API）经 :class:`JevClient` 协议注入：默认实现
:class:`HttpJevClient` 仅使用标准库（不引入第三方 HTTP 依赖）；测试可注入
:class:`StaticJevClient`，无需真实网络。

兼容 Python 3.9+。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from forgemesh.decision.provider import DecisionCandidate, DecisionRequest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: 默认请求超时（秒），与 development-plan 5.5 的决策时限语义对齐。
DEFAULT_TIMEOUT_SECONDS = 2.0


class JevError(Exception):
    """Jev 客户端基础异常（供 Provider 与 Router 统一处理）。"""


class JevUnavailableError(JevError):
    """Jev 不可用（网络/传输/未知故障）。"""


class JevTimeoutError(JevError):
    """Jev 调用超时。"""


class JevProtocolError(JevError):
    """Jev 返回结果不符合类型化契约（如 choice 不在预定义选项内）。"""


class JevQuestion(BaseModel):
    """一个 Jev 类型化判断（映射为一个候选）。

    Attributes:
        key: 问题标识，用作候选 ``candidate_id``（应唯一）。
        options: 允许的离散动作选项（类型约束，Jev 只会在其中选择）。
        parameters: 候选动作参数（选中后透传）。
        rationale: 可解释理由列表（供审计，不作为授权依据）。
        min_probability: 最低概率阈值；低于该值的判断不产出候选。
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    options: list[str] = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: list[str] = Field(default_factory=list)
    min_probability: float = Field(default=0.5, ge=0.0, le=1.0)

    def to_question(self) -> dict[str, Any]:
        """转换为 Jev API 的 question 结构（类型化选项）。"""
        return {"key": self.key, "type": "choice", "options": list(self.options)}


class JevDecision(BaseModel):
    """Jev 对单个 question 的类型化决策。

    Attributes:
        choice: 选中的动作（须落在该 question 的 ``options`` 内）。
        probability: 校准概率（0..1），供排序/解释，**绝不**作为授权凭证。
    """

    model_config = ConfigDict(extra="ignore")

    choice: str
    probability: float = Field(ge=0.0, le=1.0)


class JevClient(Protocol):
    """Jev 客户端协议（可注入，便于离线测试与替换后端）。"""

    async def query(
        self,
        state: str,
        questions: Sequence[Mapping[str, Any]],
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        """提交一次 Jev 决策查询，返回含 ``decisions`` 的原始响应。"""
        ...


class StaticJevClient:
    """离线/测试用 Jev 客户端：按预设返回决策，可选择性抛出异常或延迟。

    Args:
        decisions: ``{question_key: {"choice": ..., "probability": ...}}``。
        error: 非 ``None`` 时每次查询均抛出该异常（验证降级路径）。
        delay_s: 每次查询前的模拟延迟（秒），用于验证超时路径。
    """

    def __init__(
        self,
        decisions: Mapping[str, Any] | None = None,
        *,
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._decisions = dict(decisions) if decisions else {}
        self._error = error
        self._delay_s = delay_s

    async def query(
        self,
        state: str,
        questions: Sequence[Mapping[str, Any]],
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._error is not None:
            raise self._error
        return {"decisions": dict(self._decisions)}


class HttpJevClient:
    """基于标准库的 TypeSafe Jev HTTP 客户端。

    Args:
        endpoint: TypeSafe 决策 API 端点（如
            ``https://api.typesafe.ai/v1/decide``）。
        api_key: TypeSafe API 密钥（Bearer 认证）。
        timeout_s: 默认 HTTP 超时（秒）。

    Note:
        仅使用 :mod:`urllib`，不引入第三方 HTTP 依赖。真实的请求/响应
        schema 请以 TypeSafe 官方文档（https://docs.typesafe.ai/）为准。
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def query(
        self,
        state: str,
        questions: Sequence[Mapping[str, Any]],
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        payload = {"state": state, "questions": [dict(q) for q in questions]}
        timeout = timeout_s if timeout_s is not None else self._timeout_s
        return await asyncio.to_thread(self._post, payload, timeout)

    def _post(self, payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        """在线程中执行阻塞式 HTTP POST（不阻塞事件循环）。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            raise JevUnavailableError(f"TypeSafe Jev unavailable: {exc}") from exc
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise JevProtocolError(f"invalid Jev response: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise JevProtocolError("invalid Jev response: not an object")
        return parsed


class TypeSafeJevProvider:
    """TypeSafe Jev 决策 Provider（实现 DecisionProvider 协议）。

    Args:
        client: Jev 客户端（生产用 :class:`HttpJevClient`；测试注入离线 fake）。
        questions: 参与决策的 Jev 类型化判断列表。
        provider_id: Provider 标识（默认对齐 manifest）。
        provider_version: Provider 版本。
        timeout_s: 单次决策超时（秒），与 ``DecisionRequest.deadline_ms``
            取较小值。
    """

    provider_id: str = "forgemesh.decision.typesafe-jev"
    provider_version: str = "0.1.0"

    def __init__(
        self,
        client: JevClient,
        questions: list[JevQuestion] | None = None,
        *,
        provider_id: str | None = None,
        provider_version: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.client = client
        self.questions: list[JevQuestion] = list(questions) if questions else []
        self.timeout_s = timeout_s
        if provider_id is not None:
            self.provider_id = provider_id
        if provider_version is not None:
            self.provider_version = provider_version

    async def decide(self, req: DecisionRequest) -> Sequence[DecisionCandidate]:
        """评估所有 question，返回概率达标的候选（按概率降序）。

        无达标候选时返回空列表（Router 将按降级策略处理）。超时或 Provider
        不可用时抛出 :class:`JevError`，交由 Router 降级——**绝不**默认放行。
        """
        if not self.questions:
            return []

        state = self._build_state(req)
        payload = [q.to_question() for q in self.questions]
        timeout = self.timeout_s
        if req.deadline_ms is not None:
            timeout = min(timeout, req.deadline_ms / 1000.0)

        try:
            raw = await asyncio.wait_for(
                self.client.query(state, payload, timeout_s=timeout),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise JevTimeoutError("TypeSafe Jev decision timed out") from exc
        except JevError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任意故障均降级
            raise JevUnavailableError(f"TypeSafe Jev call failed: {exc}") from exc

        decisions = self._extract_decisions(raw)
        return self._to_candidates(decisions)

    @staticmethod
    def _build_state(req: DecisionRequest) -> str:
        """把决策请求映射为 Jev 的 ``state``。

        仅外发操作语义、风险等级与上下文（**不含** ``tenant_id``，避免外发
        租户标识）；序列化为键序稳定的 JSON 字符串，便于复现与审计。
        """
        state = {
            "operation": req.operation,
            "risk_level": req.risk_level,
            "context": req.context,
        }
        return json.dumps(state, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _extract_decisions(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        """从原始响应中取出 ``decisions`` 映射。"""
        if not isinstance(raw, Mapping):
            raise JevProtocolError("invalid Jev response: not an object")
        decisions = raw.get("decisions", raw)
        if not isinstance(decisions, Mapping):
            raise JevProtocolError("invalid Jev response: 'decisions' not an object")
        return decisions

    def _to_candidates(self, decisions: Mapping[str, Any]) -> list[DecisionCandidate]:
        """把 Jev 决策映射为候选（按概率降序）。

        Raises:
            JevProtocolError: 决策结构非法，或 ``choice`` 不在预定义选项内
                （类型安全校验失败，触发降级）。
        """
        candidates: list[DecisionCandidate] = []
        for question in self.questions:
            item = decisions.get(question.key)
            if item is None:
                continue
            try:
                decision = JevDecision.model_validate(item)
            except ValidationError as exc:
                raise JevProtocolError(f"invalid decision for {question.key!r}: {exc}") from exc
            if decision.choice not in question.options:
                raise JevProtocolError(
                    f"choice {decision.choice!r} not in options for {question.key!r}"
                )
            if decision.probability < question.min_probability:
                continue
            candidates.append(
                DecisionCandidate(
                    candidate_id=question.key,
                    action=decision.choice,
                    parameters=dict(question.parameters),
                    rationale=[
                        *question.rationale,
                        f"Jev choice={decision.choice} p={decision.probability:.3f}",
                    ],
                    confidence=decision.probability,
                )
            )
        candidates.sort(key=lambda c: c.confidence or 0.0, reverse=True)
        return candidates


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpJevClient",
    "JevClient",
    "JevDecision",
    "JevError",
    "JevProtocolError",
    "JevQuestion",
    "JevTimeoutError",
    "JevUnavailableError",
    "StaticJevClient",
    "TypeSafeJevProvider",
]
