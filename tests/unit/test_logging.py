from __future__ import annotations

import io
import json
import logging

import pytest

from research_assistant.logging import (
    bind_context,
    bound_context,
    clear_context,
    configure_logging,
    get_logger,
)
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _reset() -> None:
    clear_context()


def lines(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_json_logs_include_bound_context() -> None:
    buffer = io.StringIO()
    configure_logging(make_settings(log_json=True), stream=buffer)
    with bound_context(request_id="req-1", job_id="job-9"):
        get_logger("test").info("hello", answer=42)
    get_logger("test").info("after")
    first, second = lines(buffer)
    assert first["event"] == "hello"
    assert first["request_id"] == "req-1"
    assert first["job_id"] == "job-9"
    assert first["answer"] == 42
    assert first["level"] == "info"
    assert first["logger"] == "test"
    assert "timestamp" in first
    assert "request_id" not in second


def test_stdlib_logs_are_bridged_and_formatted() -> None:
    buffer = io.StringIO()
    configure_logging(make_settings(log_json=True), stream=buffer)
    bind_context(request_id="req-2")
    logging.getLogger("thirdparty").warning("value is %s", "x")
    try:
        raise ValueError("bad")
    except ValueError:
        logging.getLogger("thirdparty").exception("failed")
    clear_context()
    warning, error = lines(buffer)
    assert warning["event"] == "value is x"
    assert warning["level"] == "warning"
    assert warning["request_id"] == "req-2"
    assert "ValueError: bad" in str(error["exception"])


def test_console_renderer_and_level_filter() -> None:
    buffer = io.StringIO()
    configure_logging(make_settings(log_json=False, log_level="WARNING"), stream=buffer)
    log = get_logger("console", component="x")
    log.info("hidden")
    log.warning("shown")
    output = buffer.getvalue()
    assert "shown" in output
    assert "component=x" in output
    assert "hidden" not in output
    assert not output.lstrip().startswith("{")


def test_reconfiguring_does_not_duplicate_handlers() -> None:
    settings = make_settings(log_json=True)
    configure_logging(settings, stream=io.StringIO())
    buffer = io.StringIO()
    configure_logging(settings, stream=buffer)
    ours = [h for h in logging.getLogger().handlers if h.get_name() == "research_assistant"]
    assert len(ours) == 1
    get_logger().info("once")
    assert len(lines(buffer)) == 1
    uvicorn_logger = logging.getLogger("uvicorn.access")
    assert uvicorn_logger.handlers == []
    assert uvicorn_logger.propagate
