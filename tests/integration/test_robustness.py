"""Robustness tests through a run (FR-09.3): the worker reads a feature table, perturbs
it, has each variant scored only at the allow-listed scorer's ``score_url``, and records
every variant as an observation with what was perturbed and every number's n."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from rai.tevv.robustness import Scorer
from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain

pytestmark = needs_db

SCORE_URL = "http://127.0.0.1:18081/v1/score"
MODEL = {"format": "toy/1", "recipe_sha256": "abc"}


def toy(rows: list[dict[str, float | str | None]]) -> tuple[list[float], dict[str, Any]]:
    """Large web payments are fraud; without the channel, the signal is lost."""
    scores = []
    for row in rows:
        amount = row["amount"]
        big = isinstance(amount, float) and amount > 500
        scores.append(0.9 if big and row["channel"] == "web" else 0.1)
    return scores, MODEL


def feature_asset(lab: Lab, feed: Path) -> str:
    name = f"features_{uuid4().hex[:6]}"
    rows = [{"txn_id": f"T{i:05d}", "amount": f"{900 if i < 200 else 20 + i % 300}.00",
             "channel": "web" if i < 200 or i % 5 == 0 else "pos",
             "is_fraud": "true" if i < 200 else "false"} for i in range(2_000)]  # fmt: skip
    pl.DataFrame(rows).write_csv(feed / "feed" / f"{name}.csv")
    body = {"kind": "feature_set", "name": name, "locator": f"{name}.csv"}
    lab.assets[name] = lab.post(f"/v1/projects/{lab.project}/assets", body)["asset_id"]
    return name


def allow_scorer(lab: Lab, client: TestClient, **extra: Any) -> Any:
    target = {"kind": "http", "name": "model-api", "health_url": "http://127.0.0.1:18081/readyz",
              "score_url": SCORE_URL} | extra  # fmt: skip
    return client.post(f"/v1/environments/{lab.environment}/targets", json=target,
                       headers=lab.admin)  # fmt: skip


def robustness_test(lab: Lab, asset: str, **extra: Any) -> str:
    body = {
        "name": f"robust.{uuid4().hex[:6]}", "type": "robustness_check",
        "target_asset_id": lab.assets[asset],
        "definition": {
            "connector": "feed", "environment": "SANDBOX-01", "scorer": "model-api",
            "features": ["amount", "channel"], "categorical": ["channel"], "seed": 11,
            "perturbations": [
                {"name": "amount-noise", "kind": "noise", "columns": ["amount"], "scale": 0.05},
                {"name": "no-channel", "kind": "missing", "columns": ["channel"]},
            ],
            "metrics": [{"name": "recall", "max_drop": 0.1}],
        } | extra,
    }  # fmt: skip
    test_id: str = lab.post(f"/v1/projects/{lab.project}/test-cases", body)["test_id"]
    return test_id


def run(
    lab: Lab, worker_engine: Engine, feed: Path, tmp: Path, test_id: str, calls: list[str]
) -> dict[str, Any]:
    def factory(url: str) -> Scorer:
        def score(rows: list[dict[str, float | str | None]]) -> tuple[list[float], dict[str, Any]]:
            calls.append(url)
            return toy(rows)

        return score

    run_id = lab.start(lab.plan(test_id))["run_id"]
    worker = dataclasses.replace(config(feed, tmp), scorer=factory)
    drain(Worker(worker_engine, worker))
    return lab.run(run_id)


def observations(owner: Engine, run_id: str) -> dict[str, tuple[str, dict[str, Any], Any]]:
    with owner.connect() as conn:
        rows = conn.execute(
            text("SELECT rule, outcome::text, observed_value, expected FROM rai.observations "
                 "WHERE run_id = :r"), {"r": run_id},
        ).all()  # fmt: skip
    return {r.rule: (r.outcome, r.observed_value, r.expected) for r in rows}


def test_a_lost_feature_fails_with_what_was_perturbed_and_its_n(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, owner: Engine,
    tmp_path: Path,
) -> None:  # fmt: skip
    assert allow_scorer(lab, client).status_code == 201
    asset = feature_asset(lab, feed)
    calls: list[str] = []
    done = run(lab, worker_engine, feed, tmp_path, robustness_test(lab, asset), calls)
    assert (done["state"], done["verdict"]) == ("failed", "fail"), done
    assert set(calls) == {SCORE_URL}  # only the allow-listed address
    assert len(calls) == 3 * 2  # clean and two variants, 2,000 rows in batches of 1,000

    seen = observations(owner, done["run_id"])
    assert set(seen) == {"robustness.sample", "robustness.clean", "robustness.amount-noise",
                         "robustness.no-channel"}  # fmt: skip
    outcome, sample, expected = seen["robustness.sample"]
    assert outcome == "pass"
    assert (sample["rows_read"], sample["rows_used"], sample["model"]) == (2_000, 2_000, MODEL)
    assert len(sample["table_digest"]) == 64
    assert expected["max_drop"] == {"recall": 0.1}
    assert expected["seed"] == 11
    assert seen["robustness.clean"][0] == "pass"
    assert seen["robustness.amount-noise"][0] == "pass"
    outcome, lost, _ = seen["robustness.no-channel"]
    assert outcome == "fail"
    assert lost["perturbation"] == {"name": "no-channel", "kind": "missing",
                                    "columns": ["channel"], "share": 1.0, "scale": None,
                                    "value": "__unknown__"}  # fmt: skip
    assert (lost["rows_perturbed"], lost["cells_changed"]) == (2_000, 2_000)
    recall = lost["metrics"]["recall"]
    assert (recall["value"], recall["numerator"], recall["denominator"]) == (0.0, 0, 200)
    assert lost["degradations"]["recall"]["degradation"] == 1.0

    with owner.connect() as conn:
        edges = conn.execute(
            text("SELECT from_name, from_digest, from_rows FROM rai.lineage_edges "
                 "WHERE run_id = :r"), {"r": done["run_id"]},
        ).all()  # fmt: skip
    assert [(e.from_name, e.from_rows) for e in edges] == [(asset, 2_000)]
    assert edges[0].from_digest == sample["table_digest"]


def test_a_robust_model_passes_and_a_rerun_records_the_same_evidence(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, owner: Engine,
    tmp_path: Path,
) -> None:  # fmt: skip
    assert allow_scorer(lab, client).status_code == 201
    asset = feature_asset(lab, feed)
    noise_only = [{"name": "amount-noise", "kind": "noise", "columns": ["amount"], "scale": 0.05}]
    test_id = robustness_test(lab, asset, perturbations=noise_only)
    first = run(lab, worker_engine, feed, tmp_path, test_id, [])
    second = run(lab, worker_engine, feed, tmp_path, test_id, [])
    assert (first["verdict"], second["verdict"]) == ("pass", "pass")
    one, two = observations(owner, first["run_id"]), observations(owner, second["run_id"])
    assert one["robustness.amount-noise"][1] == two["robustness.amount-noise"][1]


def test_the_scorer_must_be_on_the_allow_list_with_a_score_url(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, tmp_path: Path
) -> None:
    asset = feature_asset(lab, feed)
    calls: list[str] = []
    done = run(lab, worker_engine, feed, tmp_path, robustness_test(lab, asset), calls)
    assert done["state"] == "refused"
    assert "scorer 'model-api' is not on the allow-list" in done["reason"]
    assert calls == []

    targets = f"/v1/environments/{lab.environment}/targets"
    not_http = {"kind": "db", "name": "ledger-db", "score_url": SCORE_URL}
    refused = client.post(targets, json=not_http, headers=lab.admin)
    assert (refused.status_code, refused.json()["code"]) == (422, "targets.score_not_http")
    assert allow_scorer(lab, client, score_url="file:///etc/passwd").status_code == 422
    listed = allow_scorer(lab, client)
    assert listed.json()["score_url"] == SCORE_URL


def test_a_definition_that_cannot_work_refuses_the_run(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, tmp_path: Path
) -> None:
    assert allow_scorer(lab, client).status_code == 201
    asset = feature_asset(lab, feed)
    bad = robustness_test(lab, asset, perturbations=[
        {"name": "n", "kind": "noise", "columns": ["channel"], "scale": 0.1}])  # fmt: skip
    done = run(lab, worker_engine, feed, tmp_path, bad, [])
    assert done["state"] == "refused"
    assert "noise applies to numeric features only" in done["reason"]
