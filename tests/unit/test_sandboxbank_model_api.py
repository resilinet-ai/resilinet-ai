"""SandboxBank's model-api and feature table (FR-09.3): the fraud model scores named
feature records that RAI perturbed, so RAI never runs the model itself."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sklearn")

from fastapi.testclient import TestClient

from sandboxbank import cli, model
from sandboxbank.fixtures import build_slice
from sandboxbank.model_api import MAX_ROWS, NAMES, create_app
from tests.unit.test_sandboxbank_model import REFERENCE, ROW, SMALL


@pytest.fixture(scope="module")
def small() -> model.Model:
    return model.train(SMALL)


def record(**changes: Any) -> dict[str, Any]:
    return model.feature_record(ROW, REFERENCE) | changes


def test_a_record_encodes_exactly_as_the_classifier_row() -> None:
    named = model.feature_record(ROW, REFERENCE)
    assert list(named) == NAMES
    assert named["channel"] == "ecommerce"  # categorical inputs stay readable
    assert model.encode(named) == model.features(ROW, REFERENCE)
    odd = {**ROW, "account_id": "ACC-404", "merchant_id": "", "channel": "carrier-pigeon"}
    left, right = model.encode(model.feature_record(odd, REFERENCE)), model.features(odd, REFERENCE)
    assert [math.isnan(x) for x in left] == [math.isnan(x) for x in right]
    assert [x for x in left if not math.isnan(x)] == [x for x in right if not math.isnan(x)]


def test_missing_and_unknown_inputs_are_missing_to_the_model() -> None:
    encoded = model.encode(record(amount=None, channel="__unknown__", status=None))
    names = dict(zip(NAMES, encoded, strict=True))
    assert math.isnan(names["amount"])
    assert math.isnan(names["channel"])
    assert math.isnan(names["status"])
    assert math.isnan(dict(zip(NAMES, model.encode(record(credit=True)), strict=True))["credit"])


def test_the_service_scores_records_in_order_and_names_its_model(small: model.Model) -> None:
    with TestClient(create_app(small)) as client:
        assert client.get("/readyz").json()["model"]["recipe_sha256"] == model.recipe_sha256(SMALL)
        rows = [record(), record(amount=5.0, status="posted", channel=None)]
        answer = client.post("/v1/score", json={"rows": rows})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["scores"] == small.score_records(rows)
        assert body["model"] == small.identity
        assert client.get("/v1/model").json()["features"] == NAMES


@pytest.mark.parametrize(
    ("rows", "message"),
    [([{"amount": 1.0}], "missing"),
     ([record(extra=1.0)], "unknown ['extra']"),
     ([record(channel=3.0)], "channel is categorical"),
     ([record(amount="12")], "amount is numeric"),
     ([], "at least 1"),
     ([record()] * (MAX_ROWS + 1), "at most 5000")],
)  # fmt: skip
def test_the_service_refuses_rows_that_are_not_the_model_inputs(
    small: model.Model, rows: list[dict[str, Any]], message: str
) -> None:
    with TestClient(create_app(small)) as client:
        answer = client.post("/v1/score", json={"rows": rows})
        assert answer.status_code == 422
        assert message in answer.text


def test_the_service_is_not_ready_until_trained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sandboxbank.model_api.train", lambda: None)  # never finishes
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.post("/v1/score", json={"rows": [record()]}).status_code == 503


def test_the_cli_writes_a_feature_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    build_slice(tmp_path / "slice", data_seed=27, injector_seed=28, accounts=200, merchants=40,
                rows_per_batch=2_000)  # fmt: skip
    out = tmp_path / "features.csv"
    feed = str(tmp_path / "slice" / "feed")
    assert cli.main(["model", "features", "--feed", feed, "--batch", "B000000",
                     "--out", str(out)]) == 0  # fmt: skip
    with out.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == ["txn_id", *NAMES, "is_fraud"]
    assert len(rows) == 2_000
    assert {r["is_fraud"] for r in rows} <= {"true", "false"}
    assert "2,000 transactions written as model inputs" in capsys.readouterr().out
