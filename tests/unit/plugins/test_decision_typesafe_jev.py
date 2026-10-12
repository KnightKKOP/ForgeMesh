"""decision-typesafe-jev 插件单元测试（对齐 ADR-004 / development-plan 5.5）。

覆盖 ``TypeSafeJevProvider.decide`` 的完整决策链：

* **context → state 映射**：仅外发 operation/risk_level/context，序列化为键序
  稳定 JSON，**不外发 tenant_id**（隔离不变量）。
* **候选 → questions 映射**：每个 ``JevQuestion`` 转为 ``{"key","type","options"}``
  类型化选项后作为 ``questions`` 载荷下发。
* **概率 → confidence**：Jev 校准概率映射为 ``DecisionCandidate.confidence``，
  并按概率降序排序；低于 ``min_probability`` 的判断被过滤。
* **超时降级**：超时 → ``JevTimeoutError``；JevError 透传；未知异常 →
  ``JevUnavailableError``；非法响应/越界选择 → ``JevProtocolError``（类型安全）。
* **架构不变量**（INV-1/INV-3、ADR-004/005）：决策仅为建议、不改变资源终态；
  无候选时返回空列表交 Router 降级；Jev 不可用时**绝不默认放行**（抛异常）。

另覆盖辅助类 ``JevQuestion`` / ``JevDecision`` / ``StaticJevClient`` /
``HttpJevClient`` 的行为与边界。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
from collections.abc import Mapping, Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from decision_typesafe_jev_provider import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpJevClient,
    JevDecision,
    JevError,
    JevProtocolError,
    JevQuestion,
    JevTimeoutError,
    JevUnavailableError,
    StaticJevClient,
    TypeSafeJevProvider,
)
from forgemesh.decision.provider import DecisionCandidate, DecisionRequest
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    """为每个测试提供干净的 current event loop。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _run(coro: Any) -> Any:
    """在当前（fixture 提供的）事件循环上运行协程。"""
    return asyncio.get_event_loop().run_until_complete(coro)


def _request(
    context: dict[str, Any] | None = None,
    *,
    operation: str = "inference.schedule",
    risk_level: str = "low",
    deadline_ms: int = 2000,
) -> DecisionRequest:
    """构造决策请求。"""
    return DecisionRequest(
        request_id="req-1",
        tenant_id="team-a",
        operation=operation,
        context=context if context is not None else {},
        risk_level=risk_level,  # type: ignore[arg-type]
        deadline_ms=deadline_ms,
    )


def _question(
    key: str = "q1",
    options: Sequence[str] = ("scale_up", "scale_down"),
    *,
    parameters: dict[str, Any] | None = None,
    rationale: Sequence[str] | None = None,
    min_probability: float = 0.5,
) -> JevQuestion:
    """构造一个 Jev 类型化判断。"""
    return JevQuestion(
        key=key,
        options=list(options),
        parameters=dict(parameters) if parameters else {},
        rationale=list(rationale) if rationale else [],
        min_probability=min_probability,
    )


def _client_returning(response: Any) -> MagicMock:
    """构造一个返回固定响应的注入式 Jev 客户端（AsyncMock）。"""
    client = MagicMock()
    client.query = AsyncMock(return_value=response)
    return client


class _FixedClient:
    """返回任意对象的最小 Jev 客户端（用于构造非法响应）。"""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def query(
        self,
        state: str,
        questions: Sequence[Mapping[str, Any]],
        *,
        timeout_s: float | None = None,
    ) -> Mapping[str, Any]:
        return self._response


# --------------------------------------------------------------------------- #
# Provider 标识与超时语义
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_provider_identity_defaults_and_overrides() -> None:
    """provider_id/version/timeout 默认对齐 manifest，且可被显式覆盖。"""
    provider = TypeSafeJevProvider(MagicMock())
    assert provider.provider_id == "forgemesh.decision.typesafe-jev"
    assert provider.provider_version == "0.1.0"
    assert provider.timeout_s == DEFAULT_TIMEOUT_SECONDS
    assert provider.questions == []

    custom = TypeSafeJevProvider(
        MagicMock(),
        questions=[_question()],
        provider_id="forgemesh.decision.typesafe-jev.custom",
        provider_version="9.9.9",
        timeout_s=1.5,
    )
    assert custom.provider_id == "forgemesh.decision.typesafe-jev.custom"
    assert custom.provider_version == "9.9.9"
    assert custom.timeout_s == 1.5
    assert len(custom.questions) == 1


# --------------------------------------------------------------------------- #
# decide：context→state / 候选→questions 映射
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_decide_without_questions_returns_empty_without_querying() -> None:
    """无 question 时不查询 Jev，返回空列表（Router 负责降级，不默认放行）。"""
    client = MagicMock()
    client.query = AsyncMock()
    provider = TypeSafeJevProvider(client, questions=[])

    result = _run(provider.decide(_request()))

    assert list(result) == []
    client.query.assert_not_awaited()


@pytest.mark.unit
def test_decide_maps_context_to_state_and_questions() -> None:
    """context→state 与 候选→questions 的映射、超时取 min 均需精确传递。"""
    client = _client_returning({"decisions": {"q1": {"choice": "scale_up", "probability": 0.9}}})
    provider = TypeSafeJevProvider(client, questions=[_question()], timeout_s=2.0)

    _run(
        provider.decide(
            _request(
                {"gpu_load": 0.8, "region": "cn-north"},
                operation="inference.schedule",
                risk_level="high",
                deadline_ms=1500,
            )
        )
    )

    state_arg = client.query.await_args.args[0]
    questions_arg = client.query.await_args.args[1]
    timeout_kw = client.query.await_args.kwargs["timeout_s"]

    # state == 键序稳定 JSON，仅含 operation/risk_level/context
    assert json.loads(state_arg) == {
        "operation": "inference.schedule",
        "risk_level": "high",
        "context": {"gpu_load": 0.8, "region": "cn-north"},
    }
    # 不变量：state 绝不外发 tenant_id（租户隔离）
    assert "team-a" not in state_arg
    assert "tenant_id" not in state_arg
    # 候选 → questions 类型化选项
    assert questions_arg == [{"key": "q1", "type": "choice", "options": ["scale_up", "scale_down"]}]
    # 超时 = min(provider.timeout_s=2.0, deadline_ms/1000=1.5)
    assert timeout_kw == 1.5
    client.query.assert_awaited_once()


@pytest.mark.unit
def test_decide_uses_provider_timeout_when_deadline_absent() -> None:
    """deadline_ms 为 None 时，超时应回落到 provider 自身 timeout_s。"""
    client = _client_returning({"decisions": {}})
    provider = TypeSafeJevProvider(client, questions=[_question()], timeout_s=3.0)
    req = DecisionRequest.model_construct(
        request_id="r",
        tenant_id="t",
        operation="op",
        context={},
        risk_level="low",
        deadline_ms=None,
    )

    _run(provider.decide(req))

    assert client.query.await_args.kwargs["timeout_s"] == 3.0


# --------------------------------------------------------------------------- #
# decide：概率 → confidence、排序与过滤
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_decide_maps_probability_to_confidence_and_sorts_desc() -> None:
    """概率映射为 confidence，候选按概率降序，参数/理由正确透传。"""
    client = _client_returning(
        {
            "decisions": {
                "low": {"choice": "cpu", "probability": 0.6},
                "high": {"choice": "gpu", "probability": 0.95},
            }
        }
    )
    provider = TypeSafeJevProvider(
        client,
        questions=[
            _question("low", ("cpu",), parameters={"tier": "basic"}, rationale=["low p"]),
            _question("high", ("gpu",), parameters={"tier": "premium"}, rationale=["high p"]),
        ],
    )

    result = list(_run(provider.decide(_request())))

    assert [c.candidate_id for c in result] == ["high", "low"]
    assert all(isinstance(c, DecisionCandidate) for c in result)
    top = result[0]
    assert top.action == "gpu"
    assert top.confidence == 0.95  # 概率 → confidence
    assert top.parameters == {"tier": "premium"}
    assert top.rationale[0] == "high p"
    assert top.rationale[-1] == "Jev choice=gpu p=0.950"
    assert result[1].confidence == 0.6


@pytest.mark.unit
def test_decide_skips_candidates_below_min_probability() -> None:
    """概率低于 min_probability 的判断不产出候选（含阈值边界）。"""
    client = _client_returning({"decisions": {"q1": {"choice": "scale_up", "probability": 0.4}}})
    provider = TypeSafeJevProvider(client, questions=[_question(min_probability=0.5)])

    assert list(_run(provider.decide(_request()))) == []


@pytest.mark.unit
def test_decide_includes_candidate_at_exact_min_probability() -> None:
    """概率恰好等于 min_probability 时应产出候选（边界为严格小于）。"""
    client = _client_returning({"decisions": {"q1": {"choice": "scale_up", "probability": 0.5}}})
    provider = TypeSafeJevProvider(client, questions=[_question(min_probability=0.5)])

    result = list(_run(provider.decide(_request())))

    assert len(result) == 1
    assert result[0].confidence == 0.5


@pytest.mark.unit
def test_decide_skips_questions_without_decision() -> None:
    """响应缺少某 question 的决策时跳过该 question，其余仍产出。"""
    client = _client_returning({"decisions": {"q2": {"choice": "b", "probability": 0.7}}})
    provider = TypeSafeJevProvider(
        client,
        questions=[_question("q1", ("a",)), _question("q2", ("b",))],
    )

    result = list(_run(provider.decide(_request())))

    assert [c.candidate_id for c in result] == ["q2"]


# --------------------------------------------------------------------------- #
# decide：响应解析（decisions 包裹 / 原始映射 / 非法结构）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_decide_accepts_raw_decision_mapping_without_wrapper() -> None:
    """响应未包裹 decisions 时，应回退为直接使用原始映射。"""
    client = _client_returning({"q1": {"choice": "scale_up", "probability": 0.8}})
    provider = TypeSafeJevProvider(client, questions=[_question()])

    result = list(_run(provider.decide(_request())))

    assert [c.candidate_id for c in result] == ["q1"]
    assert result[0].action == "scale_up"


@pytest.mark.unit
def test_decide_raises_protocol_error_when_response_not_object() -> None:
    """响应不是对象（如数组）→ JevProtocolError（触发降级）。"""
    provider = TypeSafeJevProvider(
        _FixedClient([1, 2, 3]),
        questions=[_question()],  # type: ignore[arg-type]
    )

    with pytest.raises(JevProtocolError, match="not an object"):
        _run(provider.decide(_request()))


@pytest.mark.unit
def test_decide_raises_protocol_error_when_decisions_not_object() -> None:
    """decisions 字段不是对象 → JevProtocolError。"""
    provider = TypeSafeJevProvider(_FixedClient({"decisions": "nope"}), questions=[_question()])

    with pytest.raises(JevProtocolError, match="'decisions' not an object"):
        _run(provider.decide(_request()))


# --------------------------------------------------------------------------- #
# decide：类型安全校验（choice 越界 / 决策结构非法）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_decide_raises_protocol_error_when_choice_not_in_options() -> None:
    """choice 落在预定义 options 之外 → JevProtocolError（类型安全失败）。"""
    client = _client_returning({"decisions": {"q1": {"choice": "nonsense", "probability": 0.9}}})
    provider = TypeSafeJevProvider(client, questions=[_question("q1", ("scale_up", "scale_down"))])

    with pytest.raises(JevProtocolError) as exc_info:
        _run(provider.decide(_request()))
    assert "choice 'nonsense' not in options for 'q1'" in str(exc_info.value)


@pytest.mark.unit
def test_decide_raises_protocol_error_when_decision_missing_probability() -> None:
    """决策缺少 probability 字段 → JevProtocolError（含 question key）。"""
    client = _client_returning({"decisions": {"q1": {"choice": "scale_up"}}})
    provider = TypeSafeJevProvider(client, questions=[_question("q1", ("scale_up",))])

    with pytest.raises(JevProtocolError, match="invalid decision for 'q1'"):
        _run(provider.decide(_request()))


@pytest.mark.unit
def test_decide_raises_protocol_error_when_probability_out_of_range() -> None:
    """概率越界（>1）无法通过校验 → JevProtocolError。"""
    client = _client_returning({"decisions": {"q1": {"choice": "scale_up", "probability": 1.5}}})
    provider = TypeSafeJevProvider(client, questions=[_question("q1", ("scale_up",))])

    with pytest.raises(JevProtocolError, match="invalid decision for 'q1'"):
        _run(provider.decide(_request()))


# --------------------------------------------------------------------------- #
# decide：超时与降级（绝不默认放行）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_decide_raises_timeout_error_when_jev_exceeds_deadline() -> None:
    """Jev 调用超过时限 → JevTimeoutError，交由 Router 降级（绝不默认放行）。"""
    provider = TypeSafeJevProvider(
        StaticJevClient(delay_s=0.05), questions=[_question()], timeout_s=0.01
    )

    with pytest.raises(JevTimeoutError) as exc_info:
        _run(provider.decide(_request()))
    assert "TypeSafe Jev decision timed out" in str(exc_info.value)


@pytest.mark.unit
def test_decide_propagates_jev_error_unchanged() -> None:
    """客户端抛出的 JevError 应原样透传，不被二次包装。"""
    client = MagicMock()
    client.query = AsyncMock(side_effect=JevUnavailableError("upstream down"))
    provider = TypeSafeJevProvider(client, questions=[_question()])

    with pytest.raises(JevUnavailableError) as exc_info:
        _run(provider.decide(_request()))
    assert str(exc_info.value) == "upstream down"


@pytest.mark.unit
def test_decide_wraps_unexpected_error_as_unavailable() -> None:
    """任意未知异常 → JevUnavailableError（统一降级入口）。"""
    client = MagicMock()
    client.query = AsyncMock(side_effect=ValueError("boom"))
    provider = TypeSafeJevProvider(client, questions=[_question()])

    with pytest.raises(JevUnavailableError) as exc_info:
        _run(provider.decide(_request()))
    assert "TypeSafe Jev call failed: boom" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 架构不变量
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_decide_is_advisory_and_does_not_mutate_request() -> None:
    """不变量：决策仅为建议——不改变请求/资源终态，动作只来自预定义选项。"""
    client = _client_returning({"decisions": {"q1": {"choice": "scale_up", "probability": 0.9}}})
    provider = TypeSafeJevProvider(client, questions=[_question("q1", ("scale_up", "scale_down"))])
    req = _request({"x": 1})
    before = req.model_dump()

    result = list(_run(provider.decide(req)))

    assert req.model_dump() == before  # 请求未被修改
    assert all(isinstance(c, DecisionCandidate) for c in result)
    assert all(c.action in ("scale_up", "scale_down") for c in result)  # 类型约束
    assert all(c.confidence is not None and 0.0 <= c.confidence <= 1.0 for c in result)


@pytest.mark.unit
def test_unavailable_jev_never_defaults_to_allow() -> None:
    """不变量：Jev 不可用时抛异常，绝不返回「默认放行」候选。"""
    client = MagicMock()
    client.query = AsyncMock(side_effect=JevUnavailableError("down"))
    provider = TypeSafeJevProvider(client, questions=[_question()])

    with pytest.raises(JevError):
        _run(provider.decide(_request()))


# --------------------------------------------------------------------------- #
# JevQuestion
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_jev_question_to_question_structure() -> None:
    """JevQuestion → Jev API question 结构（类型化 choice）。"""
    question = _question("k", ("a", "b"))
    assert question.to_question() == {"key": "k", "type": "choice", "options": ["a", "b"]}


@pytest.mark.unit
def test_jev_question_requires_at_least_one_option() -> None:
    """options 至少一个（min_length=1）。"""
    with pytest.raises(ValidationError):
        JevQuestion(key="k", options=[])


@pytest.mark.unit
def test_jev_question_min_probability_bounds() -> None:
    """min_probability 必须落在 [0,1]。"""
    with pytest.raises(ValidationError):
        JevQuestion(key="k", options=["a"], min_probability=1.5)
    with pytest.raises(ValidationError):
        JevQuestion(key="k", options=["a"], min_probability=-0.1)


# --------------------------------------------------------------------------- #
# JevDecision
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_jev_decision_probability_bounds() -> None:
    """probability 必须落在 [0,1]，端点值合法。"""
    assert JevDecision(choice="a", probability=0.0).probability == 0.0
    assert JevDecision(choice="a", probability=1.0).probability == 1.0
    with pytest.raises(ValidationError):
        JevDecision(choice="a", probability=1.5)


@pytest.mark.unit
def test_jev_decision_ignores_extra_fields() -> None:
    """extra='ignore'：未知字段不导致校验失败。"""
    decision = JevDecision.model_validate({"choice": "a", "probability": 0.5, "unexpected": 123})
    assert decision.choice == "a"
    assert decision.probability == 0.5


# --------------------------------------------------------------------------- #
# StaticJevClient
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_static_client_returns_configured_decisions() -> None:
    """StaticJevClient 返回预设 decisions 映射。"""
    client = StaticJevClient({"q1": {"choice": "a", "probability": 1.0}})
    out = _run(client.query("state", []))
    assert dict(out) == {"decisions": {"q1": {"choice": "a", "probability": 1.0}}}


@pytest.mark.unit
def test_static_client_raises_configured_error() -> None:
    """StaticJevClient 可配置抛出异常（验证降级路径）。"""
    client = StaticJevClient(error=RuntimeError("x"))
    with pytest.raises(RuntimeError, match="x"):
        _run(client.query("state", []))


# --------------------------------------------------------------------------- #
# HttpJevClient（标准库 HTTP，无第三方依赖）
# --------------------------------------------------------------------------- #


def _mock_urlopen(body: bytes) -> MagicMock:
    """构造一个支持上下文管理协议的 urlopen 返回值。"""
    response = MagicMock()
    response.read.return_value = body
    context_manager = MagicMock()
    context_manager.__enter__.return_value = response
    context_manager.__exit__.return_value = False
    return context_manager


@pytest.mark.unit
def test_http_client_query_posts_payload_and_parses_response() -> None:
    """query 应 POST 序列化载荷（Bearer 认证）并解析 JSON 响应。"""
    client = HttpJevClient("https://api.typesafe.ai/v1/decide", "secret", timeout_s=2.0)
    body = b'{"decisions": {"q1": {"choice": "a", "probability": 0.9}}}'
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(body)) as urlopen:
        out = _run(
            client.query(
                '{"operation": "op"}',
                [{"key": "q1", "type": "choice", "options": ["a"]}],
                timeout_s=5.0,
            )
        )

    assert dict(out) == {"decisions": {"q1": {"choice": "a", "probability": 0.9}}}
    request = urlopen.call_args.args[0]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8")) == {
        "state": '{"operation": "op"}',
        "questions": [{"key": "q1", "type": "choice", "options": ["a"]}],
    }
    assert urlopen.call_args.kwargs["timeout"] == 5.0  # 显式覆盖超时


@pytest.mark.unit
def test_http_client_uses_default_timeout_when_not_overridden() -> None:
    """未显式传入 timeout 时使用构造时的默认超时。"""
    client = HttpJevClient("https://api.typesafe.ai/v1/decide", "secret", timeout_s=2.0)
    with patch(
        "urllib.request.urlopen", return_value=_mock_urlopen(b'{"decisions": {}}')
    ) as urlopen:
        _run(client.query("state", []))

    assert urlopen.call_args.kwargs["timeout"] == 2.0


@pytest.mark.unit
def test_http_client_wraps_url_error_as_unavailable() -> None:
    """网络错误 → JevUnavailableError（统一降级入口）。"""
    client = HttpJevClient("https://api.typesafe.ai/v1/decide", "secret")
    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")),
        pytest.raises(JevUnavailableError, match="TypeSafe Jev unavailable"),
    ):
        _run(client.query("state", []))


@pytest.mark.unit
def test_http_client_raises_protocol_error_on_invalid_json() -> None:
    """响应体非合法 JSON → JevProtocolError。"""
    client = HttpJevClient("https://api.typesafe.ai/v1/decide", "secret")
    with (
        patch("urllib.request.urlopen", return_value=_mock_urlopen(b"not-json")),
        pytest.raises(JevProtocolError, match="invalid Jev response"),
    ):
        _run(client.query("state", []))


@pytest.mark.unit
def test_http_client_raises_protocol_error_on_non_object_json() -> None:
    """响应体为 JSON 数组（非对象）→ JevProtocolError。"""
    client = HttpJevClient("https://api.typesafe.ai/v1/decide", "secret")
    with (
        patch("urllib.request.urlopen", return_value=_mock_urlopen(b"[1, 2, 3]")),
        pytest.raises(JevProtocolError, match="not an object"),
    ):
        _run(client.query("state", []))
