"""Model evaluation through a run (FR-09.1): a scores asset is read through a worker
connector, every metric is an observation with its counts and interval, too little
data fails rather than passes, and lineage records the digest of what was judged."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl
from sqlalchemy import Engine, text

from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain

pytestmark = needs_db

TARGETS = [{"name": "recall", "at_least": 0.6}, {"name": "roc_auc", "at_least": 0.9},
           {"name": "false_positive_rate", "at_most": 0.1}]  # fmt: skip


def scores_asset(lab: Lab, feed: Path, n: int, positives: int, *, bad_label: bool = False) -> str:
    """A scores file like `sandboxbank model score` writes, registered as an asset."""
    rng = random.Random(n + positives)  # noqa: S311 - test data, not secrets
    labels = [i < positives for i in range(n)]
    scores = [min(1.0, max(0.0, rng.gauss(0.75 if y else 0.2, 0.15))) for y in labels]
    name = f"scores_{uuid4().hex[:6]}"
    pl.DataFrame({
        "txn_id": [f"T{i:06d}" for i in range(n)],
        "score": [f"{s:.6f}" for s in scores],
        "is_fraud": ["maybe" if bad_label and i == 0 else ("true" if y else "false")
                     for i, y in enumerate(labels)],
    }).write_csv(feed / "feed" / f"{name}.csv")  # fmt: skip
    body = {"kind": "table", "name": name, "locator": f"{name}.csv"}
    lab.assets[name] = lab.post(f"/v1/projects/{lab.project}/assets", body)["asset_id"]
    return name


def metric_test(lab: Lab, asset: str, **extra: Any) -> str:
    body = {
        "name": f"model.{uuid4().hex[:6]}", "type": "tevv_metric",
        "target_asset_id": lab.assets[asset],
        "definition": {"connector": "feed", "environment": "SANDBOX-01",
                       "metrics": TARGETS} | extra,
    }  # fmt: skip
    test_id: str = lab.post(f"/v1/projects/{lab.project}/test-cases", body)["test_id"]
    return test_id


def run(lab: Lab, worker_engine: Engine, feed: Path, tmp: Path, test_id: str) -> dict[str, Any]:
    run_id = lab.start(lab.plan(test_id))["run_id"]
    drain(Worker(worker_engine, config(feed, tmp)))
    return lab.run(run_id)


def observations(owner: Engine, run_id: str) -> dict[str, tuple[str, dict[str, Any], Any]]:
    with owner.connect() as conn:
        rows = conn.execute(
            text("SELECT rule, outcome::text, observed_value, expected FROM rai.observations "
                 "WHERE run_id = :r"), {"r": run_id},
        ).all()  # fmt: skip
    return {r.rule: (r.outcome, r.observed_value, r.expected) for r in rows}


def test_a_good_model_passes_with_every_number_and_its_n(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    asset = scores_asset(lab, feed, 2_000, 200)
    done = run(lab, worker_engine, feed, tmp_path, metric_test(lab, asset))
    assert (done["state"], done["verdict"]) == ("passed", "pass"), done
    seen = observations(owner, done["run_id"])
    outcome, sample, expected = seen["tevv.sample"]
    assert outcome == "pass"
    assert (sample["rows_read"], sample["rows_used"], sample["positives"]) == (2_000, 2_000, 200)
    assert sum(sample["confusion"].values()) == 2_000
    assert expected["threshold"] == 0.5
    outcome, recall, expected = seen["tevv.recall"]
    assert outcome == "pass"
    assert recall["denominator"] == 200  # recall is out of the positives
    assert recall["numerator"] == sample["confusion"]["true_positives"]
    assert recall["low"] < recall["value"] < recall["high"]
    assert recall["counts"] == "true positives of all positives"
    assert expected == {"at_least": 0.6, "at_most": None, "judge_on": "estimate",
                        "threshold": 0.5}  # fmt: skip
    assert seen["tevv.roc_auc"][1]["value"] > 0.9
    assert seen["tevv.false_positive_rate"][1]["denominator"] == 1_800
    with owner.connect() as conn:
        names = conn.execute(
            text("SELECT from_name FROM rai.lineage_edges WHERE run_id = :r AND from_digest "
                 "IS NOT NULL"), {"r": done["run_id"]},
        ).scalars().all()  # fmt: skip
    assert asset in names  # the digest of the scores that were judged


def test_a_missed_target_fails_with_its_counts(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    asset = scores_asset(lab, feed, 2_000, 200)
    strict = [{"name": "recall", "at_least": 0.999}]
    done = run(lab, worker_engine, feed, tmp_path,
               metric_test(lab, asset, metrics=strict, judge_on="confidence_bound"))  # fmt: skip
    assert (done["state"], done["verdict"]) == ("failed", "fail")
    error = done["steps"][0]["error"]
    assert error.startswith("model evaluation: recall: lower bound ")
    assert "of 200" in error
    assert observations(owner, done["run_id"])["tevv.recall"][0] == "fail"


def test_too_few_positives_fails_rather_than_passes(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    asset = scores_asset(lab, feed, 2_000, 5)
    done = run(lab, worker_engine, feed, tmp_path, metric_test(lab, asset))
    assert done["verdict"] == "fail"
    error = done["steps"][0]["error"]
    assert "too few rows to judge the model: 5 positives, at least 30" in error
    seen = observations(owner, done["run_id"])
    assert seen["tevv.sample"][0] == "error"
    assert seen["tevv.recall"][0] == "error"
    assert seen["tevv.recall"][1]["denominator"] == 5  # reported, never judged


def test_labels_that_are_not_labels_are_an_error(
    lab: Lab, feed: Path, worker_engine: Engine, owner: Engine, tmp_path: Path
) -> None:
    asset = scores_asset(lab, feed, 500, 50, bad_label=True)
    done = run(lab, worker_engine, feed, tmp_path, metric_test(lab, asset))
    assert done["verdict"] == "fail"
    assert "label(s) are not true/false or 1/0, e.g. 'maybe'" in done["steps"][0]["error"]
    assert observations(owner, done["run_id"])["tevv.sample"][0] == "error"


def test_a_bad_definition_is_refused_in_preflight(
    lab: Lab, feed: Path, worker_engine: Engine, tmp_path: Path
) -> None:
    asset = scores_asset(lab, feed, 100, 10)
    bad = [{"name": "f1", "at_least": 0.5}]
    done = run(lab, worker_engine, feed, tmp_path,
               metric_test(lab, asset, metrics=bad, judge_on="confidence_bound"))  # fmt: skip
    assert done["state"] == "refused"
    assert "the model evaluation's definition is not valid" in done["reason"]
    assert "f1 has no confidence interval" in done["reason"]
