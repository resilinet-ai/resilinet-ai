"""The baselines' failure-scenario halves and the injection matching rule (Protocol
sections 3 and 8; the author's decisions of 2026-11-25)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from rai_bench.alerts import Alert
from rai_bench.chaos import load_registry
from rai_bench.live import (
    AFTER_RESTORATION,
    B1Live,
    B2Monitor,
    Injection,
    affected,
    assign,
    b2_endpoints,
    detection_seconds,
    matches,
)

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2027, 2, 20, 10, 0, tzinfo=UTC)


def alert(arm: str, component: str, at: datetime) -> Alert:
    return Alert(arm=arm, subject_kind="injection", subject_id="live", time=at,
                 location=(component,), message=f"{component} failing")  # fmt: skip


def injection(
    components: set[str], restored: datetime | None = T0 + timedelta(minutes=3)
) -> Injection:
    return Injection("I0001", T0, restored, T0 + timedelta(minutes=20), frozenset(components))


def test_every_target_in_the_sealed_registry_belongs_to_a_component() -> None:
    registry = load_registry(ROOT / "benchmarks" / "registry" / "chaos-registry.yaml")
    for scenario in registry.scenarios:
        recovery = scenario.definition.recovery
        assert recovery is not None
        assert affected(scenario.definition.blast_radius, recovery.probes, recovery.ledger)
    s04 = next(s for s in registry.scenarios if s.id == "S04")
    recovery = s04.definition.recovery
    assert recovery is not None
    # A warehouse timeout affects the warehouse, the ETL its probe watches, and the ledger.
    assert affected(s04.definition.blast_radius, recovery.probes, recovery.ledger) == {
        "analytics-db", "etl-job", "ledger-db"}  # fmt: skip
    with pytest.raises(ValueError, match="no component"):
        affected(["somewhere-else"], [], None)


def test_an_alert_matches_inside_the_window_on_an_affected_component() -> None:
    hit = injection({"payments-api", "ledger-db"})
    assert matches(alert("B2", "payments-api", T0 + timedelta(seconds=4)), hit)
    assert not matches(alert("B2", "payments-api", T0 - timedelta(seconds=1)), hit)  # before
    late = T0 + timedelta(minutes=3) + AFTER_RESTORATION + timedelta(seconds=1)
    assert not matches(alert("B2", "payments-api", late), hit)  # after restoration + 5 min
    assert not matches(alert("B1", "feature-job", T0 + timedelta(seconds=4)), hit)  # elsewhere
    never = injection({"etl-job"}, restored=None)  # not restored: the recovery window closes it
    assert matches(alert("B1", "etl-job", T0 + timedelta(minutes=24)), never)


def test_assign_finds_matches_detection_times_and_false_alarms() -> None:
    hit = injection({"etl-job", "analytics-db"})
    alerts = [alert("B1", "etl-job", T0 + timedelta(seconds=61)),
              alert("B1", "etl-job", T0 + timedelta(seconds=121)),
              alert("B2", "payments-api", T0 + timedelta(seconds=5))]  # fmt: skip
    result = assign(alerts, [hit])
    assert [a.arm for a in result["matched"]["I0001"]["B1"]] == ["B1", "B1"]
    assert "B2" not in result["matched"]["I0001"]
    assert [a.location for a in result["false_alarms"]] == [("payments-api",)]
    assert detection_seconds(hit, result["matched"]["I0001"]["B1"]) == 61.0
    assert detection_seconds(hit, []) is None


class Switchable:
    """Endpoints whose status the test sets."""

    def __init__(self) -> None:
        self.status: dict[str, int] = {}
        self.ledger_seq = 100
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                code = stub.status.get(self.path, 200)
                body: Any = {"status": "ok"}
                if self.path == "/v1/ledger/head":
                    body = {"seq": stub.ledger_seq, "rows": stub.ledger_seq,
                            "committed_at": T0.isoformat()}  # fmt: skip
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def live() -> Iterator[Switchable]:
    stub = Switchable()
    yield stub
    stub.server.shutdown()


def test_b2_raises_one_alert_per_outage_on_liveness_only(live: Switchable) -> None:
    raised: list[Alert] = []
    monitor = B2Monitor(b2_endpoints(live.url, live.url, live.url), raised.append,
                        clock=lambda: T0)  # fmt: skip
    assert set(monitor.endpoints.values()) == {f"{live.url}/readyz", f"{live.url}/healthz"}
    monitor.poll_once()
    assert raised == []
    live.status["/healthz"] = 503  # the ETL and the feature-job stop answering liveness
    for _ in range(3):
        monitor.poll_once()
    assert sorted(a.location[0] for a in raised) == ["etl-job", "feature-job"]  # once each
    live.status.pop("/healthz")
    monitor.poll_once()
    live.status["/healthz"] = 503
    monitor.poll_once()  # a second outage is a second alert
    assert len(raised) == 4


def test_b1_live_tests_name_the_component_that_failed(live: Switchable, tmp_path: Path) -> None:
    raised: list[Alert] = []
    unreachable = "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1"
    env = {"RAI_B1_PAYMENTS": live.url, "RAI_B1_ANALYTICS_DSN": unreachable}
    b1 = B1Live(tmp_path, env, raised.append, clock=lambda: T0)
    first = b1.run_once()
    # The API answers; the warehouse is unreachable here, so its checks fail.
    assert {a.location[0] for a in first} == {"analytics-db", "etl-job", "feature-job"}
    live.status["/readyz"] = 503
    live.ledger_seq = 90  # the ledger went back: rows were lost
    second = {a.location[0]: a.message for a in b1.run_once()}
    assert "payments-api" in second
    assert "ledger seq fell from 100 to 90" in second["ledger-db"]
    assert all(a.arm == "B1" for a in raised)
