"""控制平面日志：配置取自环境变量（非法时拒绝启动）、JSON / 文本格式、记录带上请求的 ID。"""

from __future__ import annotations

import io
import json
import logging

import pytest
from forgemesh.controlplane import app as app_module
from forgemesh.controlplane.auth import AUTH_MODE_DEV, AUTH_MODE_ENV
from forgemesh.controlplane.logs import (
    LOG_FORMAT_ENV,
    LOG_LEVEL_ENV,
    JsonFormatter,
    LogConfigError,
    LogSettings,
    TextFormatter,
    configure_logging,
    log_settings_from_env,
)
from forgemesh.controlplane.outbox import OutboxEvent
from forgemesh.controlplane.publishers import EVENT_LOGGER_NAME, LogPublisher
from forgemesh.controlplane.tracecontext import request_context

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    events = logging.getLogger(EVENT_LOGGER_NAME)
    handlers, level, events_level = list(root.handlers), root.level, events.level
    yield root
    for handler in [h for h in root.handlers if h not in handlers]:
        root.removeHandler(handler)
    root.setLevel(level)
    events.setLevel(events_level)


def _epoch_record(message: str) -> logging.LogRecord:
    record = logging.LogRecord("forgemesh.test", logging.INFO, __file__, 1, message, None, None)
    record.created, record.msecs = 0.0, 0.0
    return record


@pytest.mark.unit
def test_log_settings_default_to_json_at_info() -> None:
    assert log_settings_from_env({}) == LogSettings(format="json", level="INFO")
    assert log_settings_from_env({LOG_FORMAT_ENV: "", LOG_LEVEL_ENV: ""}) == LogSettings()


@pytest.mark.unit
def test_log_settings_ignore_case_and_surrounding_spaces() -> None:
    settings = log_settings_from_env({LOG_FORMAT_ENV: " Text ", LOG_LEVEL_ENV: "debug"})

    assert settings == LogSettings(format="text", level="DEBUG")


@pytest.mark.unit
@pytest.mark.parametrize(("name", "value"), [(LOG_FORMAT_ENV, "xml"), (LOG_LEVEL_ENV, "TRACE")])
def test_invalid_log_settings_are_rejected(name: str, value: str) -> None:
    with pytest.raises(LogConfigError, match=name):
        log_settings_from_env({name: value})


@pytest.mark.unit
def test_formatters_write_utc_timestamps() -> None:
    assert json.loads(JsonFormatter().format(_epoch_record("hello"))) == {
        "time": "1970-01-01T00:00:00.000Z",
        "level": "INFO",
        "logger": "forgemesh.test",
        "message": "hello",
    }
    assert TextFormatter().format(_epoch_record("hello")) == (
        "1970-01-01 00:00:00,000 INFO forgemesh.test trace=- corr=- hello"
    )


@pytest.mark.unit
def test_json_lines_carry_the_ids_of_the_current_request(root_logger) -> None:
    stream = io.StringIO()
    configure_logging(LogSettings(format="json", level="INFO"), stream)
    logger = logging.getLogger("forgemesh.test")

    logger.info("outside %s", "request")
    with request_context("corr-1", TRACE_ID):
        logger.warning("请求内")

    outside, inside = (json.loads(line) for line in stream.getvalue().splitlines())
    assert outside["message"] == "outside request"
    assert "trace_id" not in outside
    assert "correlation_id" not in outside
    assert inside["level"] == "WARNING"
    assert inside["message"] == "请求内"
    assert (inside["trace_id"], inside["correlation_id"]) == (TRACE_ID, "corr-1")
    assert "请求内" in stream.getvalue()


@pytest.mark.unit
def test_explicit_ids_and_exceptions_are_written(root_logger) -> None:
    stream = io.StringIO()
    configure_logging(LogSettings(), stream)

    with request_context("corr-context", "a" * 32):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("forgemesh.test").exception(
                "failed", extra={"correlation_id": "corr-explicit", "trace_id": TRACE_ID}
            )

    entry = json.loads(stream.getvalue())
    assert (entry["trace_id"], entry["correlation_id"]) == (TRACE_ID, "corr-explicit")
    assert entry["level"] == "ERROR"
    assert "RuntimeError: boom" in entry["exception"]


@pytest.mark.unit
def test_text_lines_show_the_ids_or_dashes(root_logger) -> None:
    stream = io.StringIO()
    configure_logging(LogSettings(format="text", level="INFO"), stream)
    logger = logging.getLogger("forgemesh.test")

    logger.debug("hidden")
    logger.info("outside")
    with request_context("corr-1", TRACE_ID):
        logger.info("inside")

    outside, inside = stream.getvalue().splitlines()
    assert outside.endswith(" INFO forgemesh.test trace=- corr=- outside")
    assert inside.endswith(f" INFO forgemesh.test trace={TRACE_ID} corr=corr-1 inside")


@pytest.mark.unit
def test_configure_logging_replaces_only_its_own_handler(root_logger) -> None:
    other = logging.NullHandler()
    root_logger.addHandler(other)

    first = configure_logging(LogSettings(level="DEBUG"), io.StringIO())
    second = configure_logging(LogSettings(level="WARNING"), io.StringIO())

    assert other in root_logger.handlers
    assert second in root_logger.handlers
    assert first not in root_logger.handlers
    assert root_logger.level == logging.WARNING


@pytest.mark.unit
def test_events_are_written_once_in_the_log_format_whatever_the_level(root_logger, capsys) -> None:
    stream = io.StringIO()
    configure_logging(LogSettings(level="WARNING"), stream)
    publisher = LogPublisher()
    payload = {"eventId": "evt-1", "eventType": "inference.service.created"}

    logging.getLogger("forgemesh.test").info("filtered")
    publisher(
        OutboxEvent(
            aggregate_type="InferenceService",
            aggregate_id="uid-1",
            event_type="inference.service.created",
            payload=payload,
        )
    )

    (entry,) = (json.loads(line) for line in stream.getvalue().splitlines())
    assert (entry["logger"], entry["level"]) == (EVENT_LOGGER_NAME, "INFO")
    assert json.loads(entry["message"]) == payload
    assert logging.getLogger(EVENT_LOGGER_NAME).handlers == []
    assert capsys.readouterr().out == ""


@pytest.mark.unit
@pytest.mark.parametrize(("name", "value"), [(LOG_FORMAT_ENV, "xml"), (LOG_LEVEL_ENV, "loud")])
def test_main_refuses_invalid_log_configuration(monkeypatch, name: str, value: str) -> None:
    def fail_if_started(*args, **kwargs):
        raise AssertionError("server must not start")

    monkeypatch.setenv(AUTH_MODE_ENV, AUTH_MODE_DEV)
    monkeypatch.delenv(app_module.HOST_ENV, raising=False)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr("uvicorn.run", fail_if_started)

    with pytest.raises(SystemExit, match=name):
        app_module.main()


@pytest.mark.unit
def test_main_configures_logging_before_starting_uvicorn(monkeypatch, root_logger) -> None:
    started: dict[str, object] = {}

    def run(app, **kwargs):
        started["log_config"] = kwargs["log_config"]
        started["formatters"] = [type(h.formatter) for h in root_logger.handlers]
        started["level"] = root_logger.level

    monkeypatch.setenv(AUTH_MODE_ENV, AUTH_MODE_DEV)
    monkeypatch.delenv(app_module.HOST_ENV, raising=False)
    monkeypatch.delenv(app_module.PORT_ENV, raising=False)
    monkeypatch.setenv(LOG_FORMAT_ENV, "text")
    monkeypatch.setenv(LOG_LEVEL_ENV, "warning")
    monkeypatch.setattr("uvicorn.run", run)

    app_module.main()

    assert started["log_config"] is None
    assert started["formatters"].count(TextFormatter) == 1
    assert started["level"] == logging.WARNING
