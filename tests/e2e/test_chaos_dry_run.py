"""The chaos benchmark end to end on the live stack, with a short development plan
(NFR-REP-1; the author's choice of 2026-11-26).

Every unit test of the runner uses a fake RAI. This drives the real chain as the
benchmark will: ``rai-bench chaos-setup`` against the live API with two people,
``chaos-play`` through the real worker and chaos agent with B1 and B2 watching and the
seeding log feeding ground truth, beside the observer, then ``chaos-score``.

The plan is three short scenarios, one per pipeline, once each, from a development
registry (never the sealed one, whose faults last minutes):

* S01 (P1): the ledger-db-proxy connection dropped for 10 s;
* S02 (P2): the etl-job paused for 10 s (``stale_upstream``);
* S03 (P3): the feature-job restarted.

It checks that the chain works, not what it finds: every injection is run, measured,
watched by the observer and reconciled, and the score has every section. The numbers
are the benchmark's to report, never this test's to assert.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from rai_bench.chaos import build_plan
from tests.e2e.stack import AGENT, ENVIRONMENT, PAYMENTS, Stack, docker_image, eventually

ETL = os.environ.get("RAI_E2E_ETL_URL", "http://127.0.0.1:18083")
FEATURES = os.environ.get("RAI_E2E_FEATURES_URL", "http://127.0.0.1:18084")
LEDGER_DSN = "postgresql://ledger:{}@127.0.0.1:55432/ledger"
ANALYTICS_DSN = "postgresql://analytics:{}@127.0.0.1:55434/analytics"
DEV_SEED = 7_001  # development range; never an evaluation seed
PLAY_WITHIN = 2_400  # seconds for the whole plan

pytestmark = pytest.mark.skipif(
    not os.environ.get("RAI_E2E_CHAOS_DRY_RUN"),
    reason="the chaos dry run needs P3 too: make chaos-dry-run",
)


def scenario(sid: str, title: str, fault: dict[str, Any], probe: str) -> dict[str, Any]:
    return {
        "id": sid, "title": f"Dry run: {title}", "repetitions": 1,
        "definition": {
            "blast_radius": [fault["target"]],
            "faults": [fault],
            "recovery": {"probes": [probe], "ledger": "payments-ledger", "rto_seconds": 300,
                         "rpo_seconds": 0, "window_seconds": 300, "stable_checks": 3},
        },
    }  # fmt: skip


DRY_RUN = {
    "format": "rai-bench-chaos-registry/1",
    "scenarios": [
        scenario("S01", "ledger connection drop (P1)", {"type": "connection_drop",
                 "target": "ledger-db-proxy", "duration_seconds": 10}, "payments-health"),
        scenario("S02", "ETL paused (P2)", {"type": "stale_upstream", "target": "etl-job",
                 "duration_seconds": 10}, "etl-ready"),
        scenario("S03", "feature-job restart (P3)", {"type": "container_restart",
                 "target": "feature-job", "duration_seconds": 1}, "features-ready"),
    ],
}  # fmt: skip


def allow_list(admin: Any, environment_id: str) -> None:
    """Every target the dry run's scenarios name, as an admin registers them (B-22)."""
    targets = f"/v1/environments/{environment_id}/targets"
    for proxy in ("ledger-db-proxy", "analytics-db-proxy", "model-api-proxy"):
        admin.post(targets, {"kind": "proxy", "name": proxy,
                             "required_label": "rai.target=toxiproxy"})  # fmt: skip
    for name in ("payments-api", "etl-job", "feature-job"):
        admin.post(targets, {"kind": "container", "name": name,
                             "required_label": f"rai.target={name}",
                             "image_digest": docker_image(f"rai.target={name}")})  # fmt: skip
    probes = {"payments-health": f"{PAYMENTS}/readyz",
              "payments-ledger": f"{PAYMENTS}/v1/ledger/head",
              "etl-ready": f"{ETL}/readyz", "features-ready": f"{FEATURES}/readyz"}  # fmt: skip
    for name, url in probes.items():
        admin.post(targets, {"kind": "http", "name": name, "health_url": url})


@pytest.fixture
def people(stack: Stack) -> dict[str, Any]:
    """A fresh organisation: an admin, an engineer and a reviewer."""
    admin_email = f"chaos-admin-{secrets.token_hex(3)}@example.test"
    tenant_id = stack.new_tenant(admin_email)
    admin = stack.sign_in(admin_email, "admin", tenant_id)
    found: dict[str, Any] = {"admin": admin, "tenant": tenant_id}
    for role in ("engineer", "reviewer"):
        email = f"chaos-{role}-{secrets.token_hex(3)}@example.test"
        admin.post("/v1/members", {"email": email, "display_name": role.title(), "role": role})
        stack.activate(email)
        found[role] = stack.sign_in(email, role, tenant_id)
    return found


def test_the_chaos_benchmark_runs_end_to_end(
    stack: Stack, people: dict[str, Any], tmp_path: Path
) -> None:
    admin, engineer, reviewer = people["admin"], people["engineer"], people["reviewer"]
    environment = admin.post("/v1/environments", {"name": ENVIRONMENT, "type": "SANDBOX"})
    allow_list(admin, environment["environment_id"])
    project = engineer.post("/v1/projects", {
        "name": "Chaos dry run", "data_classification": "synthetic",
        "default_environment_id": environment["environment_id"],
    })  # fmt: skip
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump(DRY_RUN), encoding="utf-8")
    plan = tmp_path / "plan"
    build_plan(registry, plan, seed=DEV_SEED, min_gap_seconds=5, max_gap_seconds=5)
    run = tmp_path / "run"
    run.mkdir()

    # 1. Setup, with each person's own session.
    token = lambda person: person.http.headers["Authorization"].removeprefix("Bearer ")  # noqa: E731
    stack.cli("rai-bench", "chaos-setup", str(plan), "--out", str(run / "setup.json"),
              "--project", project["project_id"],
              "--environment-id", environment["environment_id"], "--api", stack.api_url,
              RAI_BENCH_TENANT=people["tenant"], RAI_BENCH_ENGINEER_TOKEN=token(engineer),
              RAI_BENCH_REVIEWER_TOKEN=token(reviewer))  # fmt: skip
    setup = json.loads((run / "setup.json").read_text("utf-8"))
    assert set(setup["scenarios"]) == {"S01", "S02", "S03"}
    for entry in setup["scenarios"].values():
        assert reviewer.get(f"/v1/scenarios/{entry['scenario_id']}")["status"] == "approved"

    # 2. Play, with the seeder and the observer running and B1 and B2 watching.
    key = engineer.post("/v1/api-keys", {"project_id": project["project_id"],
                                         "name": "chaos-dry-run"})["key"]  # fmt: skip
    ledger = LEDGER_DSN.format(os.environ["LEDGER_DB_PASSWORD"])
    analytics = ANALYTICS_DSN.format(os.environ["ANALYTICS_DB_PASSWORD"])
    acks, observations = tmp_path / "acks.csv", tmp_path / "observations.csv"
    with (
        stack.cli.background("observer", "sandboxbank", "observe", "--api", PAYMENTS,
                             "--etl", ETL, "--features", FEATURES, "--log", str(observations),
                             SANDBOXBANK_LEDGER_DSN=ledger),
        stack.cli.background("seeder", "sandboxbank", "seed", "--data", str(stack.sandbox),
                             "--batch", "B000001", "--log", str(acks), "--rate", "2",
                             "--api", PAYMENTS),
    ):  # fmt: skip
        eventually(lambda: acks.is_file() and observations.is_file(), "the seeder and observer")
        with stack.cli.background(
            "chaos-play", "rai-bench", "chaos-play", str(plan), str(run / "setup.json"),
            "--out", str(run), "--api", stack.api_url, "--payments", PAYMENTS, "--etl", ETL,
            "--features", FEATURES, "--acks", str(acks),
            RAI_BENCH_API_KEY=key, RAI_B1_ANALYTICS_DSN=analytics,
            RAI_TRUTH_LEDGER_DSN=ledger, RAI_TRUTH_ANALYTICS_DSN=analytics,
        ) as player:  # fmt: skip
            started = time.monotonic()
            code = player.wait(timeout=PLAY_WITHIN)
        log = (stack.cli.logs / "chaos-play.log").read_text("utf-8", errors="replace")
        took = time.monotonic() - started
        assert code == 0, f"chaos-play exited {code} after {took:.0f} s:\n{log}"

    # Nothing is left injected.
    import httpx

    assert httpx.get(f"{AGENT}/faults", timeout=5).json()["faults"] == []

    rows = [json.loads(line) for line in (run / "injections.jsonl").read_text().splitlines()]
    assert sorted(r["scenario"] for r in rows) == ["S01", "S02", "S03"]
    for row in rows:
        assert row["measured"], row  # RAI recorded a recovery measurement
        assert row["state"] in {"passed", "failed"}, row  # judged, never refused or aborted
        assert row["truth"] is not None, row  # the harness reconciled after it
        assert len(row["consistency"]) == 2, row  # both checks ran
    assert list((run / "b1").glob("b1-live-*.xml")), "B1 never ran"

    # 3. Score.
    scored = stack.cli("rai-bench", "chaos-score", str(run), str(observations),
                       "--out", str(run / "score.json"))  # fmt: skip
    result = json.loads((run / "score.json").read_text("utf-8"))
    assert result["injections"] == 3
    assert result["h3_rto"]["summary"]["unobserved"] == 0, result["h3_rto"]  # all watched
    assert result["h3_rpo"]["summary"]["scored"] == 3
    assert result["h4"]["summary"]["injections"] == 3
    assert set(result["detection"]["arms"]) >= {"B1", "B2"}
    print(scored.stdout)  # the dry run's numbers, for the log
