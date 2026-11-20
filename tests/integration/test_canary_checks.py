"""The canary privacy sentinel through a run (FR-05.1): the answer key is pinned, the
target is searched through a worker connector, a leak fails the run with every value
masked, and lineage records the digests of both."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from rai.core.telemetry import JsonFormatter
from rai.privacy.canary import Needle
from rai.privacy.sweep import sweep
from rai.worker.service import Worker
from sandboxbank.canaries import NEEDLES_FILE, generate_canaries, write_canaries
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain

pytestmark = needs_db


def asset(lab: Lab, feed: Path, frame: pl.DataFrame, prefix: str) -> str:
    name = f"{prefix}_{uuid4().hex[:6]}"
    frame.write_csv(feed / "feed" / f"{name}.csv")
    body = {"kind": "table", "name": name, "locator": f"{name}.csv"}
    lab.assets[name] = lab.post(f"/v1/projects/{lab.project}/assets", body)["asset_id"]
    return name


def canary_key(lab: Lab, feed: Path, tmp: Path) -> tuple[str, pl.DataFrame]:
    out = tmp / uuid4().hex[:6]
    write_canaries(generate_canaries(23, [f"CUS-{n:07d}" for n in range(1, 40)], 3), 23, out)
    needles = pl.read_csv(out / NEEDLES_FILE, infer_schema=False)
    return asset(lab, feed, needles, "canary_key"), needles


def canary_test(lab: Lab, target: str, key: str, **extra: Any) -> str:
    body = {
        "name": f"privacy.{uuid4().hex[:6]}", "type": "privacy_canary",
        "target_asset_id": lab.assets[target],
        "definition": {"connector": "feed", "environment": "SANDBOX-01",
                       "key": {"asset": key, "connector": "feed"}} | extra,
    }  # fmt: skip
    test_id: str = lab.post(f"/v1/projects/{lab.project}/test-cases", body)["test_id"]
    return test_id


def run(lab: Lab, worker_engine: Engine, feed: Path, tmp: Path, test_id: str) -> dict[str, Any]:
    run_id = lab.start(lab.plan(test_id))["run_id"]
    drain(Worker(worker_engine, config(feed, tmp)))
    return lab.run(run_id)


def observation(owner: Engine, run_id: str) -> tuple[str, dict[str, Any], int | None]:
    with owner.connect() as conn:
        row = conn.execute(
            text("SELECT outcome::text, observed_value, failing_count FROM rai.observations "
                 "WHERE run_id = :r AND rule = 'privacy.canary'"), {"r": run_id},
        ).one()  # fmt: skip
    return row.outcome, row.observed_value, row.failing_count


def test_a_clean_warehouse_passes_and_both_assets_are_pinned(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    key, _ = canary_key(lab, feed, tmp_path)
    clean = pl.DataFrame({"account_id": ["ACC-0000001", "ACC-0000002"],
                          "customer_id": ["CUS-0000001", "CUS-0000002"]})  # fmt: skip
    target = asset(lab, feed, clean, "dim_account")
    done = run(lab, worker_engine, feed, tmp_path, canary_test(lab, target, key))
    assert (done["state"], done["verdict"]) == ("passed", "pass"), done
    assert done["pins"]["items"][0]["reference"]["name"] == key  # pinned (NFR-REP-1)
    outcome, seen, failing = observation(owner, done["run_id"])
    assert (outcome, seen["hits"], failing) == ("pass", 0, 0)
    assert (seen["needles"], seen["canaries"]) == (21, 3)
    with owner.connect() as conn:
        names = conn.execute(
            text("SELECT from_name FROM rai.lineage_edges WHERE run_id = :r AND from_digest "
                 "IS NOT NULL"), {"r": done["run_id"]},
        ).scalars().all()  # fmt: skip
    assert {target, f"reference:{key}"} <= set(names)


def test_a_leak_fails_the_run_and_no_value_is_recorded_in_full(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, owner: Engine,
    tmp_path: Path,
) -> None:  # fmt: skip
    key, needles = canary_key(lab, feed, tmp_path)
    email = needles.filter(pl.col("kind") == "email")["value"][0]
    leaky = pl.DataFrame({"customer_id": ["CUS-0000001", "CUS-0000002"],
                          "contact": ["n/a", f"Email: {email}"]})  # fmt: skip
    target = asset(lab, feed, leaky, "export")
    done = run(lab, worker_engine, feed, tmp_path, canary_test(lab, target, key))
    assert (done["state"], done["verdict"]) == ("failed", "fail")
    error = done["steps"][0]["error"]
    assert error.startswith("privacy: 1 canary value(s) of 1 canary(ies) found in 1 row(s): email")
    assert "'contact' row 1" in error
    outcome, seen, failing = observation(owner, done["run_id"])
    assert (outcome, failing) == ("fail", 1)
    assert seen["sample"][0]["kind"] == "email"

    recorded = json.dumps(done) + json.dumps(seen)
    recorded += json.dumps(client.get(f"/v1/assurance/runs/{done['run_id']}/observations",
                                      headers=lab.viewer).json())  # fmt: skip
    leaked = [v for v in needles["value"] if v.lower() in recorded.lower()]
    assert leaked == []  # masked everywhere RAI records or returns it


def test_a_key_that_cannot_work_is_an_error(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    short = pl.DataFrame({"canary": ["c"], "kind": ["k"], "value": ["abc"]})
    key = asset(lab, feed, short, "bad_key")
    target = asset(lab, feed, pl.DataFrame({"a": ["x"]}), "t")
    done = run(lab, worker_engine, feed, tmp_path, canary_test(lab, target, key))
    assert done["verdict"] == "fail"
    assert "shorter than 6 characters" in done["steps"][0]["error"]
    assert observation(owner, done["run_id"])[0] == "error"


def test_an_unknown_key_is_refused_when_the_run_is_queued(lab: Lab, client: TestClient) -> None:
    plan = lab.plan(canary_test(lab, "batch_0", "no-such-key"))
    response = client.post("/v1/assurance/runs", json={"plan_id": plan}, headers=lab.engineer)
    assert response.status_code == 422
    assert response.json()["code"] == "runs.canary_key_unknown"


def test_a_bad_definition_is_refused_in_preflight(
    lab: Lab, feed: Path, worker_engine: Engine, tmp_path: Path
) -> None:
    key, _ = canary_key(lab, feed, tmp_path)
    done = run(lab, worker_engine, feed, tmp_path,
               canary_test(lab, "batch_0", key, columns=["a", "a"]))  # fmt: skip
    assert done["state"] == "refused"
    assert "the canary check's definition is not valid" in done["reason"]


def test_a_caught_leak_leaves_no_value_in_logs_or_the_evidence_bundle(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Part 3: a sweep of what the run leaves behind (RAI's logs, its evidence bundle)
    finds nothing, while the same sweep finds the leak in the export itself."""
    key, needles = canary_key(lab, feed, tmp_path)
    card = needles.filter(pl.col("kind") == "card_number_spaced")["value"][0]
    leaky = pl.DataFrame({"customer_id": ["CUS-0000003"], "notes": [f"paid by {card}"]})
    target = asset(lab, feed, leaky, "crm")
    with caplog.at_level(logging.DEBUG):
        done = run(lab, worker_engine, feed, tmp_path, canary_test(lab, target, key))
        bundle = client.get(f"/v1/assurance/runs/{done['run_id']}/evidence", headers=lab.viewer)
    assert done["verdict"] == "fail"
    assert bundle.status_code == 200, bundle.text
    left = tmp_path / "left-behind"
    left.mkdir()
    (left / f"rai-evidence-{done['run_id']}.zip").write_bytes(bundle.content)
    formatter = JsonFormatter("rai-test")
    (left / "rai.log").write_text(
        "\n".join(formatter.format(r) for r in caplog.records) + "\n", encoding="utf-8"
    )
    assert len(caplog.records) > 0
    found = [Needle(*row) for row in needles.iter_rows()]
    after = sweep([str(left)], found)
    assert (after.outcome, after.hits, after.sources) == ("pass", 0, 2), after.as_dict()
    assert sweep([str(feed / "feed" / f"{target}.csv")], found).hits == 1  # not blind
