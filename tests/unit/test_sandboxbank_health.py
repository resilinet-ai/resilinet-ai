"""Freshness probes for the ETL (P2) and the feature-job (P3) (B-28, review question Q1):
/readyz answers 200 only while the job is caught up, so RAI can time their recovery."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from typing import Any

import psycopg
import pytest

from sandboxbank import features
from sandboxbank.health import Readiness, serve


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_ready_only_after_a_drained_cycle_within_the_window() -> None:
    clock = Clock()
    readiness = Readiness(max_age=120, clock=clock)
    ready, body = readiness.state()
    assert (ready, body["reason"]) == (False, "no cycle has finished yet")
    readiness.cycle_succeeded()
    assert readiness.state()[0] is True
    clock.now += 119
    assert readiness.state()[0] is True
    clock.now += 2  # a stuck or paused job stops finishing cycles
    ready, body = readiness.state()
    assert ready is False
    assert "no finished cycle for more than 120 s" in body["reason"]


def test_a_failed_cycle_is_not_ready_at_once_until_the_next_success() -> None:
    clock = Clock()
    readiness = Readiness(clock=clock)
    readiness.cycle_succeeded()
    readiness.cycle_failed("model-api unavailable")
    ready, body = readiness.state()
    assert (ready, body["reason"]) == (False, "model-api unavailable")
    clock.now += 5
    readiness.cycle_succeeded()
    assert readiness.state()[0] is True


@pytest.fixture
def probe() -> Iterator[tuple[Readiness, ThreadingHTTPServer]]:
    readiness = Readiness()
    server = serve(readiness, "127.0.0.1", 0)
    yield readiness, server
    server.shutdown()


def get(server: ThreadingHTTPServer, path: str) -> tuple[int, dict[str, object]]:
    url = f"http://127.0.0.1:{server.server_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_http_probe_answers_as_the_state_says(
    probe: tuple[Readiness, ThreadingHTTPServer],
) -> None:
    readiness, server = probe
    assert get(server, "/healthz") == (200, {"status": "ok"})  # alive, though not caught up
    status, body = get(server, "/readyz")
    assert (status, body["ready"]) == (503, False)
    readiness.cycle_succeeded()
    status, body = get(server, "/readyz")
    assert (status, body["ready"]) == (200, True)
    assert get(server, "/nope")[0] == 404


def test_the_feature_job_reports_each_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    readiness = Readiness()

    def unreachable(*_: object, **__: object) -> None:
        raise features.ScoringUnavailable("refused")

    class Conn:
        def __enter__(self) -> Conn:
            return self

        def __exit__(self, *_: object) -> None:
            pass

    monkeypatch.setattr(features.psycopg, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(features, "migrate_features", lambda wh: None)
    monkeypatch.setattr(features, "run_cycle", unreachable)
    assert features.run_service("dsn", "http://x", once=True, readiness=readiness) == 1
    assert readiness.state()[1]["reason"] == "model-api unavailable"

    monkeypatch.setattr(features, "run_cycle", lambda *a, **k: features.CycleSummary())
    assert features.run_service("dsn", "http://x", once=True, readiness=readiness) == 0
    assert readiness.state()[0] is True


@pytest.mark.parametrize("job", ["etl", "features"])
def test_a_job_started_before_its_upstream_tables_exist_waits(
    job: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compose starts the payments-api, the etl-job and the feature-job together. The
    tables they read may not exist yet; that is a failed cycle, never a crash."""
    from sandboxbank import etl

    module: Any = etl if job == "etl" else features
    readiness = Readiness()

    class Conn:
        def __enter__(self) -> Conn:
            return self

        def __exit__(self, *_: object) -> None:
            pass

    def not_yet(*_: object, **__: object) -> None:
        raise psycopg.errors.UndefinedTable('relation "accounts" does not exist')

    monkeypatch.setattr(module.psycopg, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(module, "migrate_warehouse" if job == "etl" else "migrate_features",
                        lambda wh: None)  # fmt: skip
    monkeypatch.setattr(module, "run_cycle", not_yet)
    args = ("ledger", "analytics") if job == "etl" else ("analytics", "http://x")
    assert module.run_service(*args, once=True, readiness=readiness) == 1
    assert "not initialised yet" in readiness.state()[1]["reason"]
    stop = threading.Event()
    stop.set()
    assert module.run_service(*args, interval=0.01, stop=stop, readiness=readiness) == 0
    assert "_not_initialised" in capsys.readouterr().out
