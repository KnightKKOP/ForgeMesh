"""W3C ``traceparent`` 的解析与生成，以及请求上下文的绑定与恢复。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from forgemesh.controlplane import tracecontext
from forgemesh.controlplane.tracecontext import (
    TraceContext,
    current_correlation_id,
    current_trace_id,
    new_trace_context,
    parse_traceparent,
    request_context,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
VALID = f"00-{TRACE_ID}-{PARENT_ID}-01"


@pytest.mark.unit
def test_parses_a_valid_traceparent() -> None:
    context = parse_traceparent(VALID)

    assert context == TraceContext(TRACE_ID, PARENT_ID, "01")
    assert context.traceparent == VALID


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "",
        VALID[:-1],
        f" {VALID}",
        f"00-{TRACE_ID.upper()}-{PARENT_ID}-01",
        f"00-{TRACE_ID}-{PARENT_ID.upper()}-01",
        f"00-{TRACE_ID}-{PARENT_ID}-0A",
        f"00-{TRACE_ID[:-1]}g-{PARENT_ID}-01",
        f"00_{TRACE_ID}_{PARENT_ID}_01",
        f"00-{'0' * 32}-{PARENT_ID}-01",
        f"00-{TRACE_ID}-{'0' * 16}-01",
        f"ff-{TRACE_ID}-{PARENT_ID}-01",
        f"00-{TRACE_ID}-{PARENT_ID}-01-",
        f"00-{TRACE_ID}-{PARENT_ID}-01-future",
        f"cc-{TRACE_ID}-{PARENT_ID}-01x",
    ],
)
def test_rejects_invalid_traceparents(value: str) -> None:
    assert parse_traceparent(value) is None


@pytest.mark.unit
@pytest.mark.parametrize("suffix", ["", "-future-field"])
def test_parses_future_versions_by_their_version_00_prefix(suffix: str) -> None:
    context = parse_traceparent(f"cc-{TRACE_ID}-{PARENT_ID}-01{suffix}")

    assert context == TraceContext(TRACE_ID, PARENT_ID, "01")
    assert context.traceparent == VALID


@pytest.mark.unit
def test_new_trace_contexts_are_valid_unsampled_and_distinct() -> None:
    first, second = new_trace_context(), new_trace_context()

    assert parse_traceparent(first.traceparent) == first
    assert first.flags == "00"
    assert first.trace_id != second.trace_id
    assert first.parent_id != second.parent_id


@pytest.mark.unit
def test_new_trace_context_never_uses_all_zero_ids(monkeypatch) -> None:
    values = {16: iter(["0" * 32, TRACE_ID]), 8: iter(["0" * 16, PARENT_ID])}
    monkeypatch.setattr(
        tracecontext, "secrets", SimpleNamespace(token_hex=lambda nbytes: next(values[nbytes]))
    )

    assert new_trace_context() == TraceContext(TRACE_ID, PARENT_ID)


@pytest.mark.unit
def test_request_context_binds_the_ids_and_restores_the_previous_ones() -> None:
    assert (current_correlation_id(), current_trace_id()) == (None, None)

    with request_context("corr-1", TRACE_ID):
        assert (current_correlation_id(), current_trace_id()) == ("corr-1", TRACE_ID)
        with pytest.raises(RuntimeError), request_context("corr-2", "f" * 32):
            assert (current_correlation_id(), current_trace_id()) == ("corr-2", "f" * 32)
            raise RuntimeError("boom")
        assert (current_correlation_id(), current_trace_id()) == ("corr-1", TRACE_ID)

    assert (current_correlation_id(), current_trace_id()) == (None, None)
