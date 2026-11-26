"""The chaos benchmark runner (Protocol sections 3, 5, 6 and 8; B-28).

Three steps, each its own command:

1. **Setup** (:func:`setup_rai`). Publishes every registered scenario (S01 to S20)
   in RAI exactly as sealed. A second person (the reviewer) approves each one. Each
   scenario gets a plan: the scenario, then the post-recovery consistency checks of P2
   (the warehouse against the ledger) and P3 (the feature table against the warehouse).
   The author decided on 2026-11-26 that the checks run after **every** injection,
   because a P1 fault can damage the pipelines downstream. RAI is **not** given the
   seeding log: it is the ground truth for RPO and damage, so RAI's own RPO stays the
   ledger-based one it always measures. Setup writes ``setup.json``.

2. **Play** (:func:`play`). Plays the sealed plan (``load_sealed_plan``). Each injection
   starts a run of its scenario's plan no earlier than its ``start_offset_seconds``
   after the run began, and the runner waits for that run to finish before the next.
   It needs only an API key with ``runs:start`` and ``runs:read``. Meanwhile the
   baselines watch the live system (``rai_bench.live``): B1's read-only tests every
   60 s and B2's 1 s liveness monitor, their alerts appended to ``alerts.jsonl``. Each
   finished injection is one line of ``injections.jsonl`` (``rai-bench-chaos-run/1``),
   carrying RAI's recovery measurement and its consistency findings. A stopped runner
   resumes: finished injections are skipped and the start time is kept.

3. **Score** (:func:`score_chaos`). Scores these against the observer's log:
   * recovery attainment per scenario (RTO and RPO objectives met, over runs attempted);
   * H3's RTO half (``rai_bench.h3``), each injection judged on its own recovery probes;
   * the baselines' detection of each injection by Protocol section 8's matching rule,
     with false alarms.

   H3's RPO half and H4 (post-recovery damage) need ground truth from the seeding log
   and the databases. That truth is the next part.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from rai_bench.alerts import Alert, dumps, read_alerts
from rai_bench.chaos import RegisteredScenario, Registry, check_plan, load_sealed_plan
from rai_bench.h3 import score_h3
from rai_bench.live import Injection, affected, assign, detection_seconds
from rai_bench.runner import HarnessError
from sandboxbank.observer import HOLD, read, restoration

SETUP_FORMAT = "rai-bench-chaos-setup/1"
RUN_FORMAT = "rai-bench-chaos-run/1"
SCORE_FORMAT = "rai-bench-chaos-score/1"
TERMINAL = frozenset({"passed", "failed", "refused", "aborted", "error"})
CHECK_WITHIN = 900  # seconds each consistency check may wait for its target to catch up
CHECK_INTERVAL = 10.0
SLACK = timedelta(minutes=10)  # beyond faults, window and checks, before the runner gives up
POLL = 5.0
OBSERVATIONS = 200

# The worker connectors the checks read through (deployment configuration, FR-02.3).
CONNECTORS = {"ledger": "ledger", "warehouse": "warehouse", "features": "features"}


class Api:
    """A thin client of RAI's API: one bearer credential (a session token or API key)."""

    def __init__(self, base: str, credential: str, *, tenant: str | None = None,
                 http: httpx.Client | None = None) -> None:  # fmt: skip
        headers = {"Authorization": f"Bearer {credential}"}
        if tenant:
            headers["X-RAI-Tenant"] = tenant
        self.http = http or httpx.Client(base_url=base.rstrip("/"), timeout=30)
        self.http.headers.update(headers)

    def call(self, method: str, path: str, body: Any = None, **headers: str) -> Any:
        response = self.http.request(method, path, json=body, headers=headers or None)
        if response.status_code >= 400:
            raise HarnessError(f"{method} {path}: {response.status_code} {response.text[:300]}")
        return response.json() if response.content else None

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def post(self, path: str, body: Any, **headers: str) -> Any:
        return self.call("POST", path, body, **headers)


# ---- 1. setup -----------------------------------------------------------------------


def _checks(connectors: Mapping[str, str], environment: str) -> dict[str, dict[str, Any]]:
    """The post-recovery consistency checks, by name: (target asset, definition)."""
    common = {"environment": environment, "key": ["txn_id"],
              "within_seconds": CHECK_WITHIN, "interval_seconds": CHECK_INTERVAL}  # fmt: skip
    return {
        "bench.p2.consistency": {
            "target": "fact_transaction",
            "definition": common | {
                "connector": connectors["warehouse"],
                "source": {"asset": "ledger_transactions", "connector": connectors["ledger"],
                           "where": {"status": "posted"}, "rename": {"seq": "ledger_seq"}},
                "compare": ["amount", "ledger_seq"],
            },
        },
        "bench.p3.consistency": {
            "target": "fraud_features",
            "definition": common | {
                "connector": connectors["features"],
                "source": {"asset": "fact_transaction", "connector": connectors["warehouse"]},
                "compare": ["ledger_seq"],
            },
        },
    }  # fmt: skip


def _probes(scenario: RegisteredScenario) -> dict[str, Any]:
    recovery = scenario.definition.recovery
    assert recovery is not None  # noqa: S101 - RegisteredScenario guarantees it
    return {"probes": list(recovery.probes), "ledger": recovery.ledger,
            "window_seconds": recovery.window_seconds, "stable_checks": recovery.stable_checks,
            "blast_radius": sorted(scenario.definition.blast_radius)}  # fmt: skip


def _same(published: Mapping[str, Any], scenario: RegisteredScenario) -> bool:
    """RAI stored the scenario exactly as registered."""
    sealed = scenario.definition.model_dump(mode="json")
    return all(
        json.dumps(published.get(k), sort_keys=True) == json.dumps(sealed.get(k), sort_keys=True)
        for k in ("blast_radius", "faults", "recovery")
    )


def plan_registry(plan_dir: Path) -> tuple[Registry, str]:
    """A sealed plan's public registry (``registry.json``) and its hash. The answer key
    is checked against its seal but not read."""
    problems = check_plan(plan_dir)
    if problems:
        raise HarnessError(f"{plan_dir}: {'; '.join(problems)}")
    raw = json.loads((plan_dir / "registry.json").read_text(encoding="utf-8"))
    scenarios = [{k: v for k, v in s.items() if k != "sha256"} for s in raw["scenarios"]]
    manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
    return Registry.model_validate({**raw, "scenarios": scenarios}), manifest["registry_sha256"]


def setup_rai(
    engineer: Api, reviewer: Api, plan_dir: Path, out: Path, *,
    project: str, environment_id: str, environment: str = "SANDBOX-01",
    connectors: Mapping[str, str] = CONNECTORS,
) -> dict[str, Any]:  # fmt: skip
    """Publish, approve and plan every registered scenario; write ``out`` (setup.json)."""
    registry, registry_sha256 = plan_registry(plan_dir)
    if out.exists():
        raise HarnessError(f"{out} exists; set up a fresh project instead of overwriting it")
    assets: dict[str, str] = {}
    for name in ("ledger_transactions", "fact_transaction", "fraud_features"):
        made = engineer.post(f"/v1/projects/{project}/assets",
                             {"kind": "table", "name": name, "locator": name})  # fmt: skip
        assets[name] = made["asset_id"]
    hops = (("fact_transaction", "ledger_transactions", "etl-job"),
            ("fraud_features", "fact_transaction", "feature-job"))  # fmt: skip
    for target, source, transform in hops:
        engineer.post(f"/v1/assets/{assets[target]}/inputs",
                      {"input_asset_id": assets[source], "transform_ref": transform})  # fmt: skip
    checks: dict[str, str] = {}
    for name, check in _checks(connectors, environment).items():
        made = engineer.post(f"/v1/projects/{project}/test-cases", {
            "name": name, "type": "consistency_check",
            "target_asset_id": assets[check["target"]], "definition": check["definition"],
        })  # fmt: skip
        checks[name] = made["test_id"]
    scenarios: dict[str, Any] = {}
    for scenario in registry.scenarios:
        definition = scenario.definition.model_dump(mode="json")
        published = engineer.post(f"/v1/projects/{project}/scenarios", {
            "name": f"bench-{scenario.id}", "description": scenario.title,
            "blast_radius": definition["blast_radius"], "faults": definition["faults"],
            "recovery": definition["recovery"],
        })  # fmt: skip
        if not _same(published, scenario):
            raise HarnessError(f"{scenario.id}: RAI did not store the scenario as registered")
        sid = published["scenario_id"]
        reviewer.post(f"/v1/scenarios/{sid}/approvals", {
            "decision": "approve",
            "comment": f"Registered scenario {scenario.id}, sha256 {scenario.sha256()}",
        })  # fmt: skip
        items = [{"item_type": "scenario", "scenario_id": sid},
                 *({"test_id": test_id} for test_id in checks.values())]  # fmt: skip
        plan = engineer.post(f"/v1/projects/{project}/plans", {
            "name": f"bench-{scenario.id}", "items": items, "environment_id": environment_id,
        })  # fmt: skip
        scenarios[scenario.id] = {"scenario_id": sid, "scenario_sha256": scenario.sha256(),
                                  "plan_id": plan["plan_id"], **_probes(scenario)}  # fmt: skip
    result = {"format": SETUP_FORMAT, "project_id": project, "environment_id": environment_id,
              "registry_sha256": registry_sha256, "assets": assets, "checks": checks,
              "scenarios": scenarios}  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def load_setup(path: Path) -> dict[str, Any]:
    try:
        setup: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HarnessError(f"setup: {error}") from None
    if setup.get("format") != SETUP_FORMAT:
        raise HarnessError(f"{path} is not a {SETUP_FORMAT} file")
    return setup


# ---- 2. play ------------------------------------------------------------------------


class AlertLog:
    """``alerts.jsonl``, appended by both baselines' threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, alert: Alert) -> None:
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(dumps(alert) + "\n")


class Arms:
    """What runs alongside the plan: anything with ``start()`` and ``stop()``."""

    def __init__(self, *arms: Any) -> None:
        self.arms = arms

    def __enter__(self) -> Arms:
        for arm in self.arms:
            arm.start()
        return self

    def __exit__(self, *_: object) -> None:
        for arm in reversed(self.arms):
            arm.stop()


def _when(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _deadline(entry: Mapping[str, Any], window: int) -> timedelta:
    faults = sum(int(f["duration_seconds"]) for f in entry["faults"])
    return timedelta(seconds=faults + window + 2 * CHECK_WITHIN) + SLACK


def _finished(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return {row["injection_id"]: row for row in rows}


def _consistency(api: Api, run_id: str) -> dict[str, dict[str, Any]]:
    """Each check step's consistency findings: rule -> {outcome, count}."""
    found: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        query = f"limit={OBSERVATIONS}" + (f"&cursor={cursor}" if cursor else "")
        page = api.get(f"/v1/assurance/runs/{run_id}/observations?{query}")
        for o in page["items"]:
            if o["rule"].startswith("consistency."):
                rule = o["rule"].removeprefix("consistency.")
                found.setdefault(o["step_key"], {})[rule] = {
                    "outcome": o["outcome"], "count": o["failing_count"]}  # fmt: skip
        cursor = page.get("next_cursor")
        if not cursor:
            return found


def _measurement(api: Api, project: str, scenario_id: str, run: Mapping[str, Any]) -> Any:
    since = (_when(run["queued_at"]) or datetime.now(UTC)) - timedelta(minutes=1)
    path = (f"/v1/assurance/recovery?project_id={project}&scenario_id={scenario_id}"
            f"&since={since.isoformat().replace('+00:00', 'Z')}&limit=200")  # fmt: skip
    items = api.get(path)["items"]
    return next((m for m in items if m["run_id"] == run["run_id"]), None)


def play(
    api: Api, plan_dir: Path, setup: Mapping[str, Any], out: Path, *,
    expected: str | None = None, allow_evaluation: bool = False, arms: Arms | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep, poll: float = POLL,
) -> int:  # fmt: skip
    """Play every injection of the sealed plan not yet in ``injections.jsonl``; return
    how many were played now."""
    plan = load_sealed_plan(plan_dir, expected, allow_evaluation=allow_evaluation)
    manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("registry_sha256") != setup["registry_sha256"]:
        raise HarnessError("the plan and the setup come from different registries")
    out.mkdir(parents=True, exist_ok=True)
    started_file, log = out / "started.json", out / "injections.jsonl"
    if started_file.is_file():
        started = json.loads(started_file.read_text(encoding="utf-8"))
        if started["fault_log_sha256"] != manifest["fault_log_sha256"]:
            raise HarnessError(f"{out} belongs to another plan")
        t0 = _when(started["started_at"])
        assert t0 is not None  # noqa: S101 - written below
    else:
        t0 = now()
        started_file.write_text(json.dumps({
            "started_at": t0.isoformat(), "fault_log_sha256": manifest["fault_log_sha256"],
            "seed_purpose": manifest.get("seed_purpose"),
        }, indent=2) + "\n", encoding="utf-8")  # fmt: skip
    done = _finished(log)
    played = 0
    with arms or Arms():
        for entry in plan:
            if entry["injection_id"] in done:
                continue
            row = _inject(api, setup, entry, t0, now=now, sleep=sleep, poll=poll)
            with log.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            played += 1
    return played


def _inject(
    api: Api, setup: Mapping[str, Any], entry: Mapping[str, Any], t0: datetime, *,
    now: Callable[[], datetime], sleep: Callable[[float], None], poll: float,
) -> dict[str, Any]:  # fmt: skip
    scenario = setup["scenarios"].get(entry["scenario"])
    if scenario is None or scenario["scenario_sha256"] != entry["scenario_sha256"]:
        raise HarnessError(f"{entry['injection_id']}: {entry['scenario']} is not set up as sealed")
    due = t0 + timedelta(seconds=int(entry["start_offset_seconds"]))
    wait = (due - now()).total_seconds()
    if wait > 0:
        sleep(wait)
    late = max(0.0, (now() - due).total_seconds())
    key = f"{setup['registry_sha256'][:16]}-{entry['injection_id']}"
    run = api.post("/v1/assurance/runs", {"plan_id": scenario["plan_id"],
                                           "trigger_ref": f"chaos {entry['injection_id']}"},
                   **{"Idempotency-Key": key})  # fmt: skip
    give_up = now() + _deadline(entry, int(scenario["window_seconds"]))
    while run["state"] not in TERMINAL:
        if now() > give_up:
            raise HarnessError(f"{entry['injection_id']}: run {run['run_id']} has not finished; "
                               "resolve it, then resume")  # fmt: skip
        sleep(poll)
        run = api.get(f"/v1/assurance/runs/{run['run_id']}")
    measured = _measurement(api, setup["project_id"], scenario["scenario_id"], run)
    m = measured or {}
    return {
        "format": RUN_FORMAT, "injection_id": entry["injection_id"],
        "scenario": entry["scenario"], "repetition": entry["repetition"],
        "due_at": due.isoformat(), "late_seconds": round(late, 3),
        "run_id": run["run_id"], "state": run["state"], "verdict": run["verdict"],
        "reason": run.get("reason"), "queued_at": run["queued_at"],
        "finished_at": run.get("finished_at"), "measured": measured is not None,
        "onset_at": m.get("onset_at"), "reverted_at": m.get("reverted_at"),
        "restored_at": m.get("restored_at"),
        "rto_seconds": None if m.get("rto_seconds") is None else float(m["rto_seconds"]),
        "rto_objective_seconds": entry["rto_seconds"], "rto_met": m.get("rto_met"),
        "rpo_seconds": None if m.get("rpo_seconds") is None else float(m["rpo_seconds"]),
        "rpo_objective_seconds": entry["rpo_seconds"], "rpo_met": m.get("rpo_met"),
        "lost_rows": m.get("lost_rows"),
        "consistency": _consistency(api, run["run_id"]),
        "probes": scenario["probes"], "ledger": scenario["ledger"],
        "window_seconds": scenario["window_seconds"],
        "stable_checks": scenario["stable_checks"],
        "blast_radius": entry["blast_radius"],
    }  # fmt: skip


# ---- 3. score -----------------------------------------------------------------------


def read_injections(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]  # fmt: skip
    except (OSError, ValueError) as error:
        raise HarnessError(f"injections: {error}") from None
    for row in rows:
        if not isinstance(row, dict) or row.get("format") != RUN_FORMAT:
            raise HarnessError(f"{path} holds a line that is not {RUN_FORMAT}")
    return rows


def _share(met: int, n: int) -> dict[str, Any]:
    return {"met": met, "attempted": n, "share": round(met / n, 4) if n else None}


def attainment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """RTO and RPO objectives met over runs attempted, per scenario and overall (section 6:
    a run that was refused, aborted or not measured counts as attempted and not met)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by.setdefault(row["scenario"], []).append(row)

    def summary(group: list[dict[str, Any]]) -> dict[str, Any]:
        return {"runs": len(group), "measured": sum(r["measured"] for r in group),
                "rto": _share(sum(r["rto_met"] is True for r in group), len(group)),
                "rpo": _share(sum(r["rpo_met"] is True for r in group), len(group))}  # fmt: skip

    return {"overall": summary(rows),
            "scenarios": {s: summary(g) for s, g in sorted(by.items())}}  # fmt: skip


def score_chaos(injections: Path, observations: Path, alerts: Path | None = None) -> dict[str, Any]:
    """Recovery attainment, H3's RTO half, and the baselines' injection detection."""
    rows = read_injections(injections)
    if not rows:
        raise HarnessError(f"{injections} has no injections")
    try:
        samples = read(observations)
    except (OSError, ValueError) as error:
        raise HarnessError(f"observation log: {error}") from None
    holds = {max(0, int(r["stable_checks"]) - 1) * 1.0 for r in rows}
    if len(holds) > 1:
        raise HarnessError("injections use different stable_checks; score them separately")
    hold = holds.pop() if holds else HOLD
    measured = [r for r in rows if r["measured"]]
    h3 = score_h3([{
        "injection": r["injection_id"], "onset": _when(r["onset_at"]),
        "reverted": _when(r["reverted_at"]), "rto": r["rto_seconds"],
        "window": float(r["window_seconds"]), "probes": tuple(r["probes"]),
    } for r in measured], samples, hold=hold)  # fmt: skip
    h3["summary"]["not_measured"] = len(rows) - len(measured)
    result: dict[str, Any] = {"format": SCORE_FORMAT, "injections": len(rows),
                              "recovery": attainment(rows), "h3_rto": h3}  # fmt: skip
    if alerts is not None:
        result["detection"] = detection(measured, samples, read_alerts(alerts), hold=hold)
    return result


def detection(
    rows: list[dict[str, Any]], samples: list[Any], alerts: list[Alert], *, hold: float
) -> dict[str, Any]:
    """Protocol section 8: each arm's matching alerts per injection, detection times, and
    false alarms. Restoration is the observer's, on the injection's own probes."""
    injections: list[Injection] = []
    for r in rows:
        onset, reverted = _when(r["onset_at"]), _when(r["reverted_at"])
        assert onset is not None  # noqa: S101 - a measured row has both
        assert reverted is not None  # noqa: S101
        window = float(r["window_seconds"])
        truth = restoration(samples, onset, reverted, hold=hold, window=window,
                            probes=tuple(r["probes"]))  # fmt: skip
        injections.append(Injection(
            r["injection_id"], onset, truth.restored_at,
            reverted + timedelta(seconds=float(r["window_seconds"])),
            frozenset(affected(r["blast_radius"], r["probes"], r["ledger"])),
        ))  # fmt: skip
    assigned = assign(alerts, injections)
    arms = sorted({a.arm for a in alerts} | {"B1", "B2"})
    per_arm: dict[str, Any] = {}
    for arm in arms:
        times = [t for i in injections
                 if (t := detection_seconds(i, assigned["matched"].get(i.injection_id, {})
                                            .get(arm, []))) is not None]  # fmt: skip
        per_arm[arm] = {
            "detected": len(times), "injections": len(injections),
            "share": round(len(times) / len(injections), 4) if injections else None,
            "median_detection_seconds": round(statistics.median(times), 3) if times else None,
            "false_alarms": sum(a.arm == arm for a in assigned["false_alarms"]),
        }  # fmt: skip
    return {"arms": per_arm, "injections": {
        i.injection_id: {arm: detection_seconds(i, assigned["matched"].get(i.injection_id, {})
                                                .get(arm, [])) for arm in arms}
        for i in injections}}  # fmt: skip
