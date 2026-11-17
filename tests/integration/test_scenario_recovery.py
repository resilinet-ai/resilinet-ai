"""Recovery measured by the worker during a scenario (B-26b: FR-08.1, FR-08.2).

A fake system under test answers ``/health`` healthy only while the (fake) Toxiproxy
proxy is enabled, so the fault the agent applies really takes it down and the
revert really brings it back; ``/ledger`` serves a ledger head the test controls.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain
from tests.integration.test_scenarios import Chaos

pytestmark = needs_db

DROP = [{"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 1}]
RADIUS = ["ledger-db-proxy"]
FAST = {"probe_interval": 0.05}


class System:
    """The system under test: health follows the proxy; the ledger head is set by tests."""

    def __init__(self, healthy: Callable[[], bool]) -> None:
        self.healthy = healthy
        self.heads: list[dict[str, Any]] = [
            {"seq": 100, "committed_at": "2026-11-17T11:59:59Z", "rows": 100}
        ]
        system = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/health":
                    status, body = (200, {"status": "ok"}) if system.healthy() else (503, {})
                else:  # /ledger: the next head in the list, then the last one again
                    head = system.heads.pop(0) if len(system.heads) > 1 else system.heads[0]
                    status, body = 200, head
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def setup(
    lab: Lab, client: TestClient, owner: Engine, tmp_path: Path
) -> Iterator[tuple[Chaos, System]]:
    chaos = Chaos(lab, client, owner, tmp_path)
    system = System(lambda: chaos.toxiproxy.proxies["ledger-db-proxy"]["enabled"])
    targets = f"/v1/environments/{lab.environment}/targets"
    for name, path in (("payments-health", "/health"), ("payments-ledger", "/ledger")):
        target = {"kind": "http", "name": name, "health_url": system.url + path}
        added = client.post(targets, headers=lab.admin, json=target)
        assert added.status_code == 201, added.text
    yield chaos, system
    chaos.server.shutdown()
    system.server.shutdown()


def run(chaos: Chaos, worker_engine: Engine, feed: Path, tmp: Path, **recovery: Any) -> Any:
    drain(Worker(worker_engine, config(feed, tmp / "earlier")))  # leftovers: refused
    plan = {"probes": ["payments-health"], "ledger": "payments-ledger", "stable_checks": 2,
            "window_seconds": 20} | recovery  # fmt: skip
    sid = chaos.scenario(DROP, blast_radius=RADIUS, recovery=plan)["scenario_id"]
    chaos.review(sid)
    started = chaos.lab.start(chaos.plan(sid))
    assert chaos.worker(worker_engine, feed, tmp, **FAST).run_once()
    return chaos.lab.run(started["run_id"]), chaos.observations(started["run_id"])


def test_rto_and_rpo_are_measured_and_meet_their_objectives(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    chaos, system = setup
    system.heads.append({"seq": 104, "committed_at": "2026-11-17T12:00:30Z", "rows": 104})
    done, seen = run(chaos, worker_engine, feed, tmp_path, rto_seconds=30, rpo_seconds=0)
    assert (done["state"], done["verdict"]) == ("passed", "pass"), done
    kind, outcome, measured = seen["item-1/recovery.rto"]
    assert (kind, outcome) == ("metric", "pass")
    assert 1.0 <= measured["seconds"] < 10  # the fault lasted 1 s; restored right after
    assert measured["restored_at"] >= measured["reverted_at"]
    kind, outcome, ledger = seen["item-1/recovery.rpo"]
    assert (outcome, ledger["seconds"], ledger["lost_rows"]) == ("pass", 0.0, 0)
    assert (ledger["before"]["seq"], ledger["after"]["seq"]) == (100, 104)
    assert seen["item-1/recovery.ledger_before"][2]["rows"] == 100


def test_a_missed_rto_objective_fails_the_run(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    chaos, _ = setup
    done, seen = run(chaos, worker_engine, feed, tmp_path, rto_seconds=0)
    assert (done["state"], done["verdict"]) == ("failed", "fail")
    step = done["steps"][0]
    assert (step["state"], step["verdict"]) == ("done", "fail")  # measured, not broken
    assert "exceeds the objective of 0 s" in step["error"]
    assert seen["item-1/recovery.rto"][1] == "fail"


def test_no_restoration_within_the_window_fails(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    chaos, system = setup
    system.healthy = lambda: False
    done, seen = run(chaos, worker_engine, feed, tmp_path, window_seconds=1)
    assert done["verdict"] == "fail"
    assert "not restored within 1 s" in done["steps"][0]["error"]
    assert seen["item-1/recovery.rto"][2]["seconds"] is None


def test_lost_ledger_rows_set_the_rpo_and_fail(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    chaos, system = setup
    system.heads.append({"seq": 90, "committed_at": "2026-11-17T11:59:00Z", "rows": 90})
    done, seen = run(chaos, worker_engine, feed, tmp_path, rpo_seconds=3600)
    assert done["verdict"] == "fail"
    _, outcome, ledger = seen["item-1/recovery.rpo"]
    assert (outcome, ledger["lost_rows"]) == ("fail", 10)
    assert ledger["seconds"] > 0  # the recovery point is before the onset
    assert "10 committed ledger rows were lost" in done["steps"][0]["error"]


def test_probes_must_be_allow_listed(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    chaos, _ = setup
    done, _ = run(chaos, worker_engine, feed, tmp_path, probes=["somewhere-else"])
    assert done["state"] == "refused"
    assert "'somewhere-else' is not on the allow-list with a health_url" in done["reason"]
    assert chaos.agent.state.load() == {}  # refused before any fault
