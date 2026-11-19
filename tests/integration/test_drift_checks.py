"""Drift checks through a run (FR-09.2): the reference is pinned, both datasets are read
through worker connectors, every column is an observation with its sample sizes, and
lineage records the digests of what was compared."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain

pytestmark = needs_db

COLUMNS = [{"name": "amount"}, {"name": "channel", "kind": "categorical"}]


def drift_test(lab: Lab, target: str, reference: str, **extra: Any) -> str:
    body = {
        "name": f"drift.{uuid4().hex[:6]}", "type": "drift_check",
        "target_asset_id": lab.assets[target],
        "definition": {"connector": "feed", "environment": "SANDBOX-01",
                       "reference": {"asset": reference, "connector": "feed"},
                       "columns": COLUMNS} | extra,
    }  # fmt: skip
    test_id: str = lab.post(f"/v1/projects/{lab.project}/test-cases", body)["test_id"]
    return test_id


def run(lab: Lab, worker_engine: Engine, feed: Path, tmp: Path, test_id: str) -> dict[str, Any]:
    run_id = lab.start(lab.plan(test_id))["run_id"]
    drain(Worker(worker_engine, config(feed, tmp)))
    return lab.run(run_id)


def observations(owner: Engine, run_id: str) -> dict[str, tuple[str, str, dict[str, Any]]]:
    with owner.connect() as conn:
        rows = conn.execute(
            text("SELECT rule, kind::text, outcome::text, observed_value FROM rai.observations "
                 "WHERE run_id = :r"), {"r": run_id},
        ).all()  # fmt: skip
    return {r.rule: (r.kind, r.outcome, r.observed_value) for r in rows}


def test_two_batches_of_one_generator_do_not_drift(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    done = run(lab, worker_engine, feed, tmp_path, drift_test(lab, "batch_1", "batch_0"))
    assert (done["state"], done["verdict"]) == ("passed", "pass"), done
    pins = done["pins"]["items"][0]
    assert pins["reference"]["name"] == "batch_0"  # pinned with the run (NFR-REP-1)
    assert pins["reference"]["locator"] == "transactions_raw/B000000.csv"
    seen = observations(owner, done["run_id"])
    kind, outcome, amount = seen["drift.amount"]
    assert (kind, outcome) == ("metric", "pass")
    assert amount["reference_rows"] == 2_000
    assert amount["current_rows"] >= 2_000
    assert amount["psi"] < 0.1
    assert amount["band"] == "stable"
    assert 0 <= amount["ks_statistic"] < 0.1
    assert seen["drift.channel"][2]["ks_statistic"] is None  # categorical: PSI only
    with owner.connect() as conn:
        digests = conn.execute(
            text("SELECT from_name, from_digest FROM rai.lineage_edges WHERE run_id = :r"),
            {"r": done["run_id"]},
        ).all()
    names = {row.from_name for row in digests if row.from_digest}
    assert {"batch_1", "reference:batch_0"} <= names  # both, with the digest of what was read


def test_a_shifted_batch_fails_with_its_alerts(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, owner: Engine,
    tmp_path: Path,
) -> None:  # fmt: skip
    raw = feed / "feed" / "transactions_raw"
    name = f"shifted_{uuid4().hex[:6]}"
    batch = pl.read_csv(raw / "B000000.csv", infer_schema_length=0)
    batch.with_columns((pl.col("amount").cast(pl.Float64) * 3).round(2).cast(pl.String)).write_csv(
        raw / f"{name}.csv"
    )
    body = {"kind": "table", "name": name, "locator": f"transactions_raw/{name}.csv"}
    asset = lab.post(f"/v1/projects/{lab.project}/assets", body)
    lab.assets[name] = asset["asset_id"]
    done = run(lab, worker_engine, feed, tmp_path, drift_test(lab, name, "batch_0"))
    assert (done["state"], done["verdict"]) == ("failed", "fail")
    [step] = done["steps"]
    assert step["error"].startswith("drift: amount: PSI ")
    assert "KS D" in step["error"]
    seen = observations(owner, done["run_id"])
    assert seen["drift.amount"][1] == "fail"
    assert seen["drift.amount"][2]["band"] == "significant shift"
    assert seen["drift.channel"][1] == "pass"  # amounts moved, channels did not

    failing = client.get(f"/v1/assurance/runs/{done['run_id']}/observations?outcome=fail",
                         headers=lab.viewer).json()["items"]  # fmt: skip
    assert [o["rule"] for o in failing] == ["drift.amount"]


def test_too_little_data_fails_rather_than_passes(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    done = run(lab, worker_engine, feed, tmp_path,
               drift_test(lab, "batch_1", "batch_0", min_rows=1_000_000))  # fmt: skip
    assert done["verdict"] == "fail"
    assert "too few values to judge drift" in done["steps"][0]["error"]
    assert observations(owner, done["run_id"])["drift.amount"][1] == "error"


def test_an_unknown_reference_is_refused_when_the_run_is_queued(
    lab: Lab, client: TestClient
) -> None:
    plan = lab.plan(drift_test(lab, "batch_1", "no-such-asset"))
    response = client.post("/v1/assurance/runs", json={"plan_id": plan}, headers=lab.engineer)
    assert response.status_code == 422
    assert response.json()["code"] == "runs.drift_reference_unknown"


def test_a_bad_definition_is_refused_in_preflight(
    lab: Lab, feed: Path, worker_engine: Engine, tmp_path: Path
) -> None:
    done = run(lab, worker_engine, feed, tmp_path,
               drift_test(lab, "batch_1", "batch_0", columns=[]))  # fmt: skip
    assert done["state"] == "refused"
    assert "the drift check's definition is not valid" in done["reason"]
