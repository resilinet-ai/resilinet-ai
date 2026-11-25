"""Defect fixtures for P2 and P3 (B-28, Protocol section 4; author's decision 2026-11-24:
all twelve classes on every pipeline, adapted). P2 delivers warehouse loads
(fact_transaction), P3 chunks of the fraud feature table (fraud_features)."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sklearn")

from rai.validation.contract import load_contract
from rai.validation.engine import read_batch, validate
from rai_bench.cli import _arms
from rai_bench.runner import HarnessError, delivered_table
from sandboxbank import model
from sandboxbank.fixtures import build_fixtures
from sandboxbank.injector import RATES
from sandboxbank.schema import PIPELINE_TABLES, column_names
from tests.unit.test_sandboxbank_model import SMALL as SMALL_RECIPE

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts" / "sandboxbank"
SIZE = {"accounts": 300, "merchants": 40, "rows_per_batch": 1_500, "batches_per_class": 1,
        "clean": 2}  # fmt: skip


@pytest.fixture(scope="module")
def small_model() -> model.Model:
    return model.train(SMALL_RECIPE)


@pytest.fixture(scope="module")
def sets(tmp_path_factory: pytest.TempPathFactory, small_model: model.Model) -> dict[str, Path]:
    built = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(model, "train", lambda recipe=None: small_model)
        for pipeline in ("P2", "P3"):
            out = tmp_path_factory.mktemp(pipeline)
            build_fixtures(out, data_seed=31, injector_seed=32, pipeline=pipeline, **SIZE)
            built[pipeline] = out
    return built


def truth(out: Path) -> list[dict[str, Any]]:
    lines = (out / "answer_key" / "ground_truth.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def rows(out: Path, record: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    with (out / record["file"]).open(newline="", encoding="utf-8") as handle:
        header, *data = list(csv.reader(handle))
    return header, data


@pytest.mark.parametrize("pipeline", ["P2", "P3"])
def test_every_class_on_every_pipeline_with_its_own_table(
    sets: dict[str, Path], pipeline: str
) -> None:
    out = sets[pipeline]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    table = PIPELINE_TABLES[pipeline]
    assert (manifest["pipeline"], manifest["table"]) == (pipeline, table)
    assert delivered_table(manifest) == table
    records = truth(out)
    by_class = Counter(r["defect_class"] for r in records)
    assert set(by_class) == {None, *RATES}
    clean = next(r for r in records if r["defect_class"] is None)
    header, data = rows(out, clean)
    assert header == list(column_names(table))
    assert {row[header.index("txn_id")] for row in data}.__len__() == len(data)
    if pipeline == "P3":
        assert manifest["model"]["recipe_sha256"] == model.recipe_sha256(SMALL_RECIPE)
        assert {row[header.index("status")] for row in data} == {"posted"}


@pytest.mark.parametrize("pipeline", ["P2", "P3"])
def test_the_contract_passes_clean_loads_and_fails_every_defective_one(
    sets: dict[str, Path], pipeline: str
) -> None:
    # Arm R's own contract on the fixtures: no false alarm on a clean batch, and every
    # defective batch fails at least one rule (the dry run's recall, without the scorer).
    out = sets[pipeline]
    contract = load_contract(CONTRACTS / f"{PIPELINE_TABLES[pipeline]}.yaml")
    references = {"accounts": read_batch(out / "feed" / "accounts.csv"),
                  "merchants": read_batch(out / "feed" / "merchants.csv")}  # fmt: skip
    for record in truth(out):
        if record["defect_class"] in ("D02", "D11"):
            continue  # needs the key history across batches: the harness's dry run covers it
        if record["defect_class"] == "D12" and pipeline == "P2":
            continue  # a merchants-dimension defect, checked by the dimension's contract
        report = validate(contract, read_batch(out / record["file"]), references=references)
        failed = [r.rule_id for r in report.results if r.outcome != "pass"
                  and not r.rule_id.startswith("unique_across_batches")]  # fmt: skip
        if record["defect_class"] is None:
            assert failed == [], (record["batch_id"], failed)
        else:
            assert failed, (record["defect_class"], record["batch_id"])


def test_p3_classes_are_adapted_as_the_author_decided(sets: dict[str, Path]) -> None:
    out = sets["P3"]
    catalogue = {m["category"] for m in csv.DictReader((out / "feed" / "merchants.csv").open())}
    for record in truth(out):
        header, data = rows(out, record)
        col = {name: i for i, name in enumerate(header)}
        hit = [data[i] for i in record["rows"]]
        if record["defect_class"] == "D05":
            for row in hit:
                score, amount = float(row[col["score"]]), float(row[col["amount"]])
                assert amount < 0 or not 0 <= score <= 1
        elif record["defect_class"] == "D06":
            assert record["columns"] == ["merchant_category"]
            assert all(row[col["has_merchant"]] == "1" for row in hit)
            assert all(row[col["merchant_category"]] not in catalogue for row in hit)
        elif record["defect_class"] == "D12":
            assert record["columns"] == ["txn_id", "account_type"]
            assert record["detail"]["dimension"] == "accounts"
            for i in record["rows"]:
                twin = [r for j, r in enumerate(data)
                        if r[0] == data[i][0] and j != i]  # fmt: skip
                assert len(twin) == 1
                assert twin[0][col["account_type"]] != data[i][col["account_type"]]
        elif record["defect_class"] == "D04":
            assert record["columns"] == ["score"]
        elif record["defect_class"] == "D10":
            assert record["columns"] == ["scored_at"]


def test_p2_rows_are_posted_transactions_in_ledger_order(sets: dict[str, Path]) -> None:
    out = sets["P2"]
    seen: list[int] = []
    for record in truth(out):
        if record["defect_class"] not in (None,):
            continue
        header, data = rows(out, record)
        seq, created, committed = (header.index(c) for c in
                                   ("ledger_seq", "created_at", "committed_at"))  # fmt: skip
        seen += [int(r[seq]) for r in data]
        assert all(r[committed] > r[created] for r in data)
    assert seen == sorted(seen)


def test_an_unknown_pipeline_and_baselines_without_a_suite_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pipeline must be one of"):
        build_fixtures(tmp_path / "x", data_seed=31, injector_seed=32, pipeline="P4", **SIZE)
    with pytest.raises(HarnessError, match="arm B1 has no suite for fact_transaction yet"):
        _arms(["B1"], CONTRACTS, "fact_transaction")
    assert delivered_table({}) == "transactions_raw"  # P1 sets made before B-28
