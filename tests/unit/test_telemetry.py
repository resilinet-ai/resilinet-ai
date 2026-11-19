"""Structured logs, trace context and health endpoints (NFR-OPS-1)."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from chaos_agent import logs as agent_logs
from rai.api.app import create_app
from rai.api.settings import Settings
from rai.core.telemetry import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    current_span,
    parse_traceparent,
    span,
)
from rai.worker.executor import WorkerConfig
from rai.worker.service import Worker, serve_health

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"


class Capture(logging.Handler):
    """Every record, formatted as JSON the way the services log it."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter("test"))
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def captured() -> Iterator[Capture]:
    handler = Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield handler
    root.removeHandler(handler)
    root.setLevel(old_level)


def spans(capture: Capture) -> list[dict[str, Any]]:
    return [line for line in capture.lines if line.get("event") == "span"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (f"00-{TRACE}-{PARENT}-01", (TRACE, PARENT)),
        (f"  00-{TRACE.upper()}-{PARENT}-00 ", (TRACE, PARENT)),
        (None, None),
        ("", None),
        (f"01-{TRACE}-{PARENT}-01", None),  # unknown version
        (f"00-{'0' * 32}-{PARENT}-01", None),  # all-zero trace id
        (f"00-{TRACE}-{'0' * 16}-01", None),  # all-zero parent
        (f"00-{TRACE[:-1]}-{PARENT}-01", None),
        ("00-not-hex-at-all", None),
    ],
)
def test_traceparent_is_parsed_strictly(header: str | None, expected: object) -> None:
    assert parse_traceparent(header) == expected


def test_spans_nest_continue_a_caller_and_log_once_when_they_end(captured: Capture) -> None:
    with span("outer", traceparent=f"00-{TRACE}-{PARENT}-01", kind="test") as outer:
        assert current_span() is outer
        assert outer.traceparent == f"00-{TRACE}-{outer.span_id}-01"
        with span("inner") as inner:
            logging.getLogger("rai.test").info("inside", extra={"fields": {"run_id": "r-1"}})
        assert current_span() is outer
    assert current_span() is None
    assert (outer.trace_id, outer.parent_id) == (TRACE, PARENT)
    assert (inner.trace_id, inner.parent_id) == (TRACE, outer.span_id)

    inside = next(line for line in captured.lines if line["message"] == "inside")
    assert (inside["trace_id"], inside["span_id"], inside["run_id"]) == (
        TRACE,
        inner.span_id,
        "r-1",
    )
    first, second = spans(captured)
    assert (first["span"], first["span_id"], first["parent_span_id"]) == (
        "inner",
        inner.span_id,
        outer.span_id,
    )
    assert (second["span"], second["kind"], second["status"]) == ("outer", "test", "ok")
    assert second["duration_ms"] >= 0


def test_a_new_trace_starts_without_a_valid_parent_and_errors_are_marked(captured: Capture) -> None:
    with pytest.raises(RuntimeError), span("work", traceparent="garbage") as this:
        raise RuntimeError("boom")
    assert this.parent_id is None
    assert len(this.trace_id) == 32
    [logged] = spans(captured)
    assert (logged["status"], logged["parent_span_id"]) == ("error", None)


def test_json_lines_carry_the_service_level_time_and_errors(captured: Capture) -> None:
    try:
        raise ValueError("bad input")
    except ValueError:
        logging.getLogger("rai.test").exception("it failed", extra={"fields": {"level": "x"}})
    [line] = captured.lines
    assert line["service"] == "test"
    assert line["level"] == "error"  # a field cannot overwrite the record's own keys
    assert line["logger"] == "rai.test"
    assert line["time"].endswith("Z")
    assert line["error"] == {"type": "ValueError", "message": "bad input"}
    assert "Traceback" in line["stack"]


def test_extra_record_attributes_are_kept_but_terminal_colours_are_not(captured: Capture) -> None:
    logging.getLogger("uvicorn.error").info(
        "running", extra={"color_message": "[1mrunning[0m", "port": 8000}
    )
    [line] = captured.lines
    assert line["port"] == 8000
    assert "color_message" not in line


def test_configure_logging_formats_and_levels(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("rai-test", {"RAI_LOG_LEVEL": "warning"})
        logging.getLogger("rai.test").info("hidden")
        logging.getLogger("rai.test").warning("shown")
        [line] = capsys.readouterr().err.splitlines()
        assert json.loads(line)["message"] == "shown"
        configure_logging("rai-test", {"RAI_LOG_FORMAT": "text"})
        assert isinstance(root.handlers[0].formatter, TextFormatter)
        for bad in ({"RAI_LOG_FORMAT": "xml"}, {"RAI_LOG_LEVEL": "LOUD"}):
            with pytest.raises(ValueError, match="RAI_LOG_"):
                configure_logging("rai-test", bad)
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])


def test_text_lines_show_the_trace() -> None:
    record = logging.LogRecord("rai.test", logging.INFO, "", 0, "hello", None, None)
    assert TextFormatter().format(record).endswith("INFO rai.test hello")
    with span("t", traceparent=f"00-{TRACE}-{PARENT}-01"):
        assert f"[{TRACE[:8]}] hello" in TextFormatter().format(record)


# ---- the API: spans, traceparent, health and readiness ---------------------------------


@pytest.fixture
def offline_api() -> TestClient:
    """The app without its start-up check, over a database that is not there."""
    engine = create_engine(
        "postgresql+psycopg://nobody:none@127.0.0.1:9/none", connect_args={"connect_timeout": 1}
    )
    settings = Settings(database_url="unused", jwt_secret="x" * 40)
    return TestClient(create_app(settings, engine=engine))


def test_api_requests_are_spans_that_continue_the_callers_trace(
    offline_api: TestClient, captured: Capture
) -> None:
    response = offline_api.get("/healthz", headers={"traceparent": f"00-{TRACE}-{PARENT}-01"})
    assert response.json() == {"status": "ok"}
    trace = parse_traceparent(response.headers["traceparent"])
    assert trace is not None
    assert trace[0] == TRACE
    assert trace[1] != PARENT
    [logged] = spans(captured)
    assert (logged["span"], logged["route"], logged["status_code"]) == (
        "http.request",
        "/healthz",
        200,
    )
    assert (logged["trace_id"], logged["parent_span_id"], logged["method"]) == (
        TRACE,
        PARENT,
        "GET",
    )

    fresh = offline_api.get("/nowhere")
    assert fresh.status_code == 404
    new = parse_traceparent(fresh.headers["traceparent"])
    assert new is not None
    assert new[0] != TRACE
    assert spans(captured)[-1]["route"] == "unmatched"


def test_too_large_requests_are_spans_too(offline_api: TestClient, captured: Capture) -> None:
    response = offline_api.post(
        "/v1/auth/login", content=b"x" * 10, headers={"content-length": "99999999"}
    )
    assert response.status_code == 413
    assert "traceparent" in response.headers
    assert spans(captured)[-1]["status_code"] == 413


def test_readyz_reports_an_unreachable_database(offline_api: TestClient, captured: Capture) -> None:
    response = offline_api.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}
    warning = next(line for line in captured.lines if line["message"].startswith("not ready"))
    assert warning["error_type"] == "OperationalError"
    assert "none@" not in json.dumps(captured.lines)  # no connection string in the log
    assert spans(captured)[-1]["status"] == "error"


# ---- the worker's health port -------------------------------------------------------


def get(url: str) -> tuple[int, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_worker_health_follows_its_heartbeat_and_the_database(tmp_path: Path) -> None:
    worker = Worker(create_engine("sqlite://"), WorkerConfig(name="w-test", state_dir=tmp_path))
    server = serve_health(worker, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert get(f"{base}/healthz") == (200, {"status": "ok"})
        assert get(f"{base}/readyz") == (200, {"status": "ok"})
        worker.heartbeat = time.monotonic() - 301  # silent for longer than a lease
        assert get(f"{base}/healthz") == (503, {"status": "unavailable"})
        assert get(f"{base}/readyz")[0] == 503
        worker.heartbeat = time.monotonic()
        worker.engine = create_engine("postgresql+psycopg://nobody:none@127.0.0.1:9/none",
                                      connect_args={"connect_timeout": 1})  # fmt: skip
        assert get(f"{base}/healthz")[0] == 200
        assert get(f"{base}/readyz")[0] == 503
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/other", timeout=5)  # noqa: S310
    finally:
        server.shutdown()


# ---- the chaos agent and watchdog --------------------------------------------------


def test_the_agent_logs_the_same_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        agent_logs.configure("rai-chaos-agent", {})
        agent_log = logging.getLogger("rai.chaos_agent")
        agent_log.warning("refused: %s", "outside the radius",
                          extra={"fields": {"step": "item-1"}})  # fmt: skip
        line = json.loads(capsys.readouterr().err.strip())
        assert (line["service"], line["level"], line["step"]) == (
            "rai-chaos-agent",
            "warning",
            "item-1",
        )
        assert line["message"] == "refused: outside the radius"
        agent_logs.configure("rai-chaos-agent", {"RAI_LOG_FORMAT": "text"})
        assert not isinstance(root.handlers[0].formatter, agent_logs.JsonFormatter)
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])
