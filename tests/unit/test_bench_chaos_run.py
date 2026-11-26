"""The chaos benchmark runner: setup, play and score (B-28; the author's decisions of
2026-11-25 and 2026-11-26)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from rai.api.routes_scenarios import NAME
from rai_bench.alerts import Alert, write_alerts
from rai_bench.chaos import build_plan
from rai_bench.chaos_run import AlertLog, Api, Arms, load_setup, play, score_chaos, setup_rai
from rai_bench.cli import app
from rai_bench.runner import HarnessError
from sandboxbank.observer import ObservationLog, Sample

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2027, 2, 20, 10, 0, tzinfo=UTC)


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def plan_dir(tmp_path: Path) -> Path:
    """A development plan of two sealed scenarios, once each: S01 (P1) and S04 (P2)."""
    sealed = yaml.safe_load((ROOT / "benchmarks/registry/chaos-registry.yaml").read_text("utf-8"))
    picked = [s | {"repetitions": 1} for s in sealed["scenarios"] if s["id"] in {"S01", "S04"}]
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump(sealed | {"scenarios": picked}), encoding="utf-8")
    out = tmp_path / "plan"
    build_plan(registry, out, seed=4_242, min_gap_seconds=60, max_gap_seconds=60)
    return out


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeRai:
    """Enough of RAI's API for the runner: it records what it was sent."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sent: list[tuple[str, str, Any, dict[str, str]]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.polls: dict[str, int] = {}
        self.plans: dict[str, str] = {}  # plan id -> scenario id
        self.scenarios: dict[str, dict[str, Any]] = {}
        self.tamper = False

    def client(self, who: str) -> Api:
        transport = httpx.MockTransport(lambda r: self.handle(who, r))
        return Api("http://rai", f"token-{who}", tenant="t1",
                   http=httpx.Client(base_url="http://rai", transport=transport))  # fmt: skip

    def handle(self, who: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path, method = request.url.path, request.method
        self.sent.append((who, f"{method} {path}", body, dict(request.headers)))
        new = str(uuid4())
        named = method == "POST" and isinstance(body, dict) and "name" in body
        if named and not re.fullmatch(NAME, body["name"]):  # RAI's own name rule
            return httpx.Response(422, json={"code": "request.invalid", "name": body["name"]})
        if method == "POST" and path.endswith("/assets"):
            return httpx.Response(201, json={"asset_id": new})
        if method == "POST" and (path.endswith("/inputs") or path.endswith("/approvals")):
            return httpx.Response(201, json={"ok": True})
        if method == "POST" and path.endswith("/test-cases"):
            return httpx.Response(201, json={"test_id": new})
        if method == "POST" and path.endswith("/scenarios"):
            stored = dict(body) | {"scenario_id": new, "status": "pending"}
            if self.tamper:
                stored["faults"] = [dict(body["faults"][0], duration_seconds=1)]
            self.scenarios[new] = stored
            return httpx.Response(201, json=stored)
        if method == "POST" and path.endswith("/plans"):
            self.plans[new] = body["items"][0]["scenario_id"]
            return httpx.Response(201, json={"plan_id": new})
        if method == "POST" and path == "/v1/assurance/runs":
            key = request.headers["Idempotency-Key"]
            if key not in self.keys:
                self.keys[key] = new
                self.runs[new] = {"run_id": new, "plan_id": body["plan_id"], "state": "queued",
                                  "verdict": None, "queued_at": stamp(self.clock())}  # fmt: skip
            return httpx.Response(202, json=self.runs[self.keys[key]])
        if method == "GET" and path.startswith("/v1/assurance/runs/") and path.count("/") == 4:
            run = self.runs[path.rsplit("/", 1)[1]]
            self.polls[run["run_id"]] = self.polls.get(run["run_id"], 0) + 1
            if self.polls[run["run_id"]] >= 3:
                run.update(state="passed", verdict="pass", finished_at=stamp(self.clock()))
            return httpx.Response(200, json=run)
        if path.endswith("/observations"):
            items = [{"step_key": "item-2", "rule": "consistency.missing", "outcome": "pass",
                      "failing_count": 0},
                     {"step_key": "item-3", "rule": "consistency.duplicated", "outcome": "fail",
                      "failing_count": 2},
                     {"step_key": "item-1", "rule": "recovery.rto", "outcome": "pass",
                      "failing_count": None}]  # fmt: skip
            return httpx.Response(200, json={"items": items, "next_cursor": None})
        if path == "/v1/assurance/recovery":
            items = []
            for run in self.runs.values():
                queued = datetime.fromisoformat(run["queued_at"].replace("Z", "+00:00"))
                onset = queued + timedelta(seconds=5)
                items.append({"run_id": run["run_id"], "onset_at": stamp(onset),
                              "reverted_at": stamp(onset + timedelta(seconds=300)),
                              "restored_at": stamp(onset + timedelta(seconds=310)),
                              "rto_seconds": "310.000", "rto_met": True, "rpo_seconds": "0",
                              "rpo_met": True, "lost_rows": 0})  # fmt: skip
            return httpx.Response(200, json={"items": items})
        return httpx.Response(404, json={"title": f"no fake for {method} {path}"})


def make_setup(rai: FakeRai, plan_dir: Path, tmp_path: Path) -> dict[str, Any]:
    return setup_rai(rai.client("engineer"), rai.client("reviewer"), plan_dir,
                     tmp_path / "setup.json", project="p1", environment_id="e1")  # fmt: skip


def test_setup_publishes_approves_and_plans_each_registered_scenario(
    plan_dir: Path, tmp_path: Path
) -> None:
    rai = FakeRai(Clock())
    setup = make_setup(rai, plan_dir, tmp_path)
    assert load_setup(tmp_path / "setup.json") == setup
    assert set(setup["scenarios"]) == {"S01", "S04"}
    assert setup["scenarios"]["S04"]["probes"] == ["etl-ready"]
    manifest = json.loads((plan_dir / "manifest.json").read_text("utf-8"))
    assert setup["registry_sha256"] == manifest["registry_sha256"]
    # Separation of duties: the engineer publishes, the reviewer approves.
    approvals = [who for who, call, _, _ in rai.sent if call.endswith("/approvals")]
    assert approvals == ["reviewer", "reviewer"]
    published = [who for who, call, _, _ in rai.sent if call.endswith("/scenarios")]
    assert published == ["engineer", "engineer"]
    # Every plan: the scenario, then the P2 and P3 checks. No seeding-log check.
    checks = [body for _, call, body, _ in rai.sent if call.endswith("/test-cases")]
    assert [c["name"] for c in checks] == ["bench.p2.consistency", "bench.p3.consistency"]
    assert all("rpo" not in c["definition"] for c in checks)
    assert checks[0]["definition"]["source"]["asset"] == "ledger_transactions"
    plans = [body for _, call, body, _ in rai.sent if call.endswith("/plans")]
    assert all(len(p["items"]) == 3 and p["environment_id"] == "e1" for p in plans)
    assert all(p["items"][0]["item_type"] == "scenario" for p in plans)
    with pytest.raises(HarnessError, match="exists"):
        make_setup(rai, plan_dir, tmp_path)


def test_setup_refuses_a_scenario_rai_stored_differently(plan_dir: Path, tmp_path: Path) -> None:
    rai = FakeRai(Clock())
    rai.tamper = True
    with pytest.raises(HarnessError, match="not store the scenario as registered"):
        make_setup(rai, plan_dir, tmp_path)


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


def test_play_runs_each_injection_on_schedule_and_resumes(plan_dir: Path, tmp_path: Path) -> None:
    clock = Clock()
    rai = FakeRai(clock)
    setup = make_setup(rai, plan_dir, tmp_path)
    out, arm = tmp_path / "run", Recorder()
    played = play(rai.client("key"), plan_dir, setup, out, arms=Arms(arm), now=clock,
                  sleep=clock.sleep, poll=5)  # fmt: skip
    assert played == 2
    assert arm.events == ["start", "stop"]
    rows = [json.loads(line) for line in (out / "injections.jsonl").read_text().splitlines()]
    fault_log = (plan_dir / "answer_key" / "fault_log.jsonl").read_text().splitlines()
    planned = [json.loads(line) for line in fault_log]
    assert [r["injection_id"] for r in rows] == [p["injection_id"] for p in planned]
    for row, entry in zip(rows, planned, strict=True):
        # Never earlier than its offset from the start of the run.
        queued = datetime.fromisoformat(row["queued_at"].replace("Z", "+00:00"))
        assert queued >= T0 + timedelta(seconds=entry["start_offset_seconds"])
        assert row["state"] == "passed"
        assert row["rto_seconds"] == 310.0
        assert row["rto_objective_seconds"] == entry["rto_seconds"]
        assert row["consistency"] == {
            "item-2": {"missing": {"outcome": "pass", "count": 0}},
            "item-3": {"duplicated": {"outcome": "fail", "count": 2}},
        }
        assert row["probes"] == setup["scenarios"][row["scenario"]]["probes"]
    starts = [h for _, call, _, h in rai.sent if call == "POST /v1/assurance/runs"]
    assert len({h["idempotency-key"] for h in starts}) == 2
    assert all(h["authorization"] == "Bearer token-key" for h in starts)
    # Resuming plays nothing twice, and keeps the start time.
    assert play(rai.client("key"), plan_dir, setup, out, now=clock, sleep=clock.sleep) == 0
    assert json.loads((out / "started.json").read_text())["started_at"] == T0.isoformat()


def test_play_refuses_a_mismatched_setup_or_a_stuck_run(plan_dir: Path, tmp_path: Path) -> None:
    clock = Clock()
    rai = FakeRai(clock)
    setup = make_setup(rai, plan_dir, tmp_path)
    with pytest.raises(HarnessError, match="different registries"):
        play(rai.client("key"), plan_dir, setup | {"registry_sha256": "0" * 64},
             tmp_path / "a", now=clock, sleep=clock.sleep)  # fmt: skip
    rai.polls = {}
    stuck = FakeRai(clock)
    stuck.handle = _never_finishes(stuck)  # type: ignore[method-assign]
    with pytest.raises(HarnessError, match="has not finished"):
        play(stuck.client("key"), plan_dir, setup, tmp_path / "b", now=clock,
             sleep=clock.sleep, poll=60)  # fmt: skip


def _never_finishes(rai: FakeRai) -> Any:
    handle = rai.handle

    def wrapped(who: str, request: httpx.Request) -> httpx.Response:
        response = handle(who, request)
        if request.method == "GET" and request.url.path.count("/") == 4:
            run = rai.runs[request.url.path.rsplit("/", 1)[1]]
            run.update(state="running", verdict=None)
            return httpx.Response(200, json=run)
        return response

    return wrapped


def injection_row(n: int, onset: datetime, rto: float | None, *, probes: list[str],
                  blast: list[str], met: bool = True) -> dict[str, Any]:  # fmt: skip
    return {"format": "rai-bench-chaos-run/1", "injection_id": f"I{n:04d}",
            "scenario": "S04" if probes == ["etl-ready"] else "S01", "measured": True,
            "onset_at": stamp(onset), "reverted_at": stamp(onset + timedelta(seconds=60)),
            "rto_seconds": rto, "rto_met": met, "rpo_met": True, "window_seconds": 600,
            "stable_checks": 3, "probes": probes, "ledger": "payments-ledger",
            "blast_radius": blast}  # fmt: skip


def test_score_gives_attainment_h3_and_detection(tmp_path: Path) -> None:
    # I0001 (S01): the API down 0-80 s. I0002 (S04): the ETL behind 200-300 s, API up.
    log = ObservationLog(tmp_path / "obs.csv")
    for i in range(int(400 / 0.25)):
        t = i * 0.25
        api = 0 if t < 80 else 200
        etl = 503 if 200 <= t < 300 else 200
        log.append(Sample(T0 + timedelta(seconds=t), api, True, i, etl=etl, features=200))
    rows = [
        injection_row(1, T0, 80.5, probes=["payments-health"], blast=["ledger-db-proxy"]),
        injection_row(2, T0 + timedelta(seconds=200), 90.0, probes=["etl-ready"],
                      blast=["analytics-db-proxy"], met=False),
        {**injection_row(3, T0, None, probes=["etl-ready"], blast=["etl-job"]),
         "measured": False, "onset_at": None, "reverted_at": None, "rto_met": None},
    ]  # fmt: skip
    (tmp_path / "injections.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def alert(arm: str, component: str, seconds: float) -> Alert:
        return Alert(arm=arm, subject_kind="injection", subject_id="live",
                     time=T0 + timedelta(seconds=seconds), location=(component,),
                     message="failing")  # fmt: skip

    write_alerts(tmp_path / "alerts.jsonl", [
        alert("B2", "payments-api", 2), alert("B1", "etl-job", 260),
        alert("B2", "feature-job", 900),
    ])  # fmt: skip
    result = score_chaos(tmp_path / "injections.jsonl", tmp_path / "obs.csv",
                         tmp_path / "alerts.jsonl")  # fmt: skip
    overall = result["recovery"]["overall"]
    assert (overall["runs"], overall["measured"]) == (3, 2)
    assert overall["rto"] == {"met": 1, "attempted": 3, "share": 0.3333}
    assert result["recovery"]["scenarios"]["S04"]["rto"]["attempted"] == 2
    by_id = {r["injection"]: r for r in result["h3_rto"]["injections"]}
    assert by_id["I0001"]["truth_rto_seconds"] == 80.0  # the API's own probe
    assert by_id["I0002"]["truth_rto_seconds"] == 100.0  # the ETL's, not the API's
    assert by_id["I0002"]["agrees"] is False
    assert result["h3_rto"]["summary"]["not_measured"] == 1
    arms = result["detection"]["arms"]
    assert (arms["B2"]["detected"], arms["B2"]["false_alarms"]) == (1, 1)
    assert arms["B2"]["median_detection_seconds"] == 2.0
    assert (arms["B1"]["detected"], arms["B1"]["median_detection_seconds"]) == (1, 60.0)
    assert result["detection"]["injections"]["I0002"] == {"B1": 60.0, "B2": None}

    printed = CliRunner().invoke(app, ["chaos-score", str(tmp_path), str(tmp_path / "obs.csv"),
                                       "--out", str(tmp_path / "score.json")])  # fmt: skip
    assert printed.exit_code == 0, printed.output
    assert "RTO met 1/3" in printed.output
    assert "B1: detected 1/2" in printed.output
    assert json.loads((tmp_path / "score.json").read_text())["format"] == "rai-bench-chaos-score/1"


def test_alert_log_appends_from_threads(tmp_path: Path) -> None:
    import threading

    from rai_bench.alerts import read_alerts

    sink = AlertLog(tmp_path / "alerts.jsonl")
    made = [Alert(arm="B2", subject_kind="injection", subject_id="live", time=T0,
                  location=("payments-api",), message=str(n)) for n in range(50)]  # fmt: skip
    threads = [threading.Thread(target=sink, args=(a,)) for a in made]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(a.message for a in read_alerts(sink.path)) == sorted(a.message for a in made)


def test_the_cli_needs_its_secrets_from_the_environment(
    plan_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("RAI_BENCH_API_KEY", "RAI_BENCH_TENANT"):
        monkeypatch.delenv(name, raising=False)
    played = CliRunner().invoke(app, ["chaos-play", str(plan_dir), str(tmp_path / "s.json"),
                                      "--out", str(tmp_path / "run")])  # fmt: skip
    assert played.exit_code == 2
    assert "set RAI_BENCH_API_KEY" in played.output
    setup = CliRunner().invoke(app, ["chaos-setup", str(plan_dir), "--out", str(tmp_path / "s"),
                                     "--project", "p", "--environment-id", "e"])  # fmt: skip
    assert setup.exit_code == 2
    assert "set RAI_BENCH_TENANT" in setup.output


def test_play_keeps_the_ground_truth_of_each_injection(plan_dir: Path, tmp_path: Path) -> None:
    clock = Clock()
    rai = FakeRai(clock)
    setup = make_setup(rai, plan_dir, tmp_path)
    seen: list[str] = []

    def truth(row: dict[str, Any]) -> dict[str, Any]:
        seen.append(row["injection_id"])  # after RAI's run finished
        assert row["state"] == "passed"
        return {"lost": False, "damaged": False, "new": {}, "rpo_truth_seconds": 0.0}

    play(rai.client("key"), plan_dir, setup, tmp_path / "run", now=clock, sleep=clock.sleep,
         truth=truth)  # fmt: skip
    rows = [json.loads(x) for x in (tmp_path / "run" / "injections.jsonl").read_text().splitlines()]
    assert [r["injection_id"] for r in rows] == seen
    assert all(r["truth"]["damaged"] is False for r in rows)
