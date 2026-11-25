"""Baseline arms B1 (pytest) and B2 (Great Expectations) (B-21, Protocol section 3).

Defects here are made by hand on small clean SandboxBank data, never taken from
defect fixtures: these tests check that each arm runs and reports what its own
checks see, not how well it scores.
"""

from __future__ import annotations

import csv
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rai_bench.arms import Delivery
from rai_bench.baselines import (
    TIGHTENINGS,
    B1Arm,
    B2Arm,
    _columns_named,
    b1_claim,
    profile_suite,
    tighten,
    write_suites,
)
from sandboxbank.dataset import write_dataset
from sandboxbank.generator import DEFAULT_START, GeneratorConfig

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("clean")
    write_dataset(GeneratorConfig(seed=3, accounts=200, merchants=40, batches=4,
                                  rows_per_batch=300), out)  # fmt: skip
    return out


def arrival(index: int) -> datetime:
    return DEFAULT_START + timedelta(seconds=900 * (index + 1))


def rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write(path: Path, header: list[str], body: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(body)
    return path


def defective(dataset: Path, out: Path, change: str) -> Path:
    """Batch B000001 with one hand-made defect."""
    header, body = rows(dataset / "transactions_raw" / "B000001.csv")
    if change == "duplicate":
        body.append(dict(body[0]))
    elif change == "missing":
        body[3]["amount"] = ""
    elif change == "type":
        body[4]["amount"] = "12,50"
    elif change == "unknown_account":
        body[5]["account_id"] = "ACC-9999999"
    elif change == "negative":
        body[6]["amount"] = "-3.00"
    elif change == "late":
        body[7]["created_at"] = "2025-12-31T23:00:00.000Z"
    elif change == "bad_timestamp":
        body[8]["created_at"] = "yesterday"
    elif change == "renamed":
        header = [("amt" if c == "amount" else c) for c in header]
        body = [{("amt" if k == "amount" else k): v for k, v in r.items()} for r in body]
    elif change == "dropped":
        header = [c for c in header if c != "status"]
        body = [{k: v for k, v in r.items() if k != "status"} for r in body]
    elif change == "previous":
        shutil.copy(dataset / "transactions_raw" / "B000000.csv", out / "previous.csv")
        return out / "previous.csv"
    return write(out / f"{change}.csv", header, body)


def delivery(file: Path, dataset: Path, dimension: Path | None = None) -> Delivery:
    return Delivery("B000001", file, dataset, arrival(1), dimension)


def merchants_with_duplicate(dataset: Path, out: Path) -> Path:
    header, body = rows(dataset / "merchants.csv")
    return write(out / "merchants_dup.csv", header, [*body, body[0]])


def observe(arm: Any, dataset: Path, file: Path, tmp: Path, dimension: Path | None = None) -> Any:
    state = tmp / f"state-{arm.name}-{file.stem}"
    state.mkdir(parents=True, exist_ok=True)
    arm.open(state, dataset)
    try:
        return arm.observe(delivery(file, dataset, dimension), lambda: NOW)
    finally:
        arm.close()


def claims(alerts: Any) -> set[tuple[tuple[str, ...], str | None]]:
    return {(a.location, a.defect_class) for a in alerts}


# ---- B1 --------------------------------------------------------------------------------------


def test_b1_is_quiet_on_a_clean_batch_and_its_dimension(dataset: Path, tmp_path: Path) -> None:
    clean = dataset / "transactions_raw" / "B000001.csv"
    assert observe(B1Arm(), dataset, clean, tmp_path, dataset / "merchants.csv") == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("duplicate", {(("txn_id",), "D01")}),
        ("missing", {(("amount",), "D03")}),
        ("type", {(("amount",), "D04")}),
        ("renamed", {(("amount", "amt"), "D07")}),
        ("dropped", {(("status",), "D08")}),
        ("unknown_account", set()),  # outside B1's scope (Protocol section 3)
        ("negative", set()),
    ],
)
def test_b1_reports_what_its_tests_check(
    dataset: Path, tmp_path: Path, change: str, expected: set[Any]
) -> None:
    alerts = observe(B1Arm(), dataset, defective(dataset, tmp_path, change), tmp_path)
    assert claims(alerts) == expected
    assert all(a.arm == "B1" and a.subject_id == "B000001" for a in alerts)


def test_b1_checks_the_delivered_dimension(dataset: Path, tmp_path: Path) -> None:
    clean = dataset / "transactions_raw" / "B000001.csv"
    alerts = observe(B1Arm(), dataset, clean, tmp_path, merchants_with_duplicate(dataset, tmp_path))
    assert claims(alerts) == {(("merchant_id",), "D12")}
    assert alerts[0].message.startswith("merchants dimension: test_primary_key: merchant_id: 1 of")


def test_b1_claims_and_messages() -> None:
    assert _columns_named("missing: ['amount']; unexpected: ['amt']") == ("amount", "amt")
    assert b1_claim("test_columns", "transactions_raw", "missing: []; unexpected: ['x']") is None
    assert b1_claim("test_unknown", "transactions_raw", "") is None


# ---- B2 --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def suites(dataset: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("great_expectations")
    out = tmp_path_factory.mktemp("suites")
    write_suites(dataset, out)
    return out


def test_profiling_follows_the_documented_rules(suites: Path) -> None:
    import json

    profiled = json.loads((suites / "transactions_raw.profiled.json").read_text("utf-8"))
    kinds = {(e["type"], e["kwargs"].get("column")) for e in profiled["expectations"]}
    assert ("expect_column_values_to_be_unique", "txn_id") in kinds
    assert ("expect_column_values_to_not_be_null", "amount") in kinds
    assert ("expect_column_values_to_not_be_null", "merchant_id") not in kinds  # has blanks
    assert ("expect_column_values_to_be_in_set", "channel") in kinds
    assert ("expect_column_values_to_be_in_set", "is_fraud") not in kinds  # booleans: type only
    assert ("expect_column_values_to_be_between", "amount") in kinds
    assert all(e["meta"] == {"source": "profiled"} for e in profiled["expectations"])

    final = json.loads((suites / "transactions_raw.suite.json").read_text("utf-8"))
    amount_bounds = [e for e in final["expectations"]
                     if e["type"] == "expect_column_values_to_be_between"
                     and e["kwargs"]["column"] == "amount"]  # fmt: skip
    assert [e["meta"]["id"] for e in amount_bounds] == ["K5"]  # the over-fitted range is replaced
    tightened = [
        e["meta"]["id"] for e in final["expectations"] if e["meta"]["source"] == "tightened"
    ]
    assert tightened == [t["id"] for t in TIGHTENINGS["transactions_raw"] if "type" in t]
    assert ("expect_column_values_to_be_in_type_list", "created_at") in kinds  # profiled...
    assert not [e for e in final["expectations"]  # ...and removed by R1, with its reason
                if e["type"] == "expect_column_values_to_be_in_type_list"
                and e["kwargs"]["column"] == "created_at"]  # fmt: skip
    assert all(t["spec"] for table in TIGHTENINGS.values() for t in table)


def test_tighten_is_a_pure_function_of_the_profile() -> None:
    import pandas as pd

    profiled = profile_suite([pd.DataFrame({"amount": [1.0, 2.0]})], "t")
    assert tighten(profiled, "merchants")["expectations"][:-1] == profiled["expectations"]


def test_b2_is_quiet_on_a_clean_batch_and_its_dimension(
    dataset: Path, suites: Path, tmp_path: Path
) -> None:
    clean = dataset / "transactions_raw" / "B000001.csv"
    assert observe(B2Arm(suites), dataset, clean, tmp_path, dataset / "merchants.csv") == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("missing", {(("amount",), "D03")}),
        ("type", {(("amount",), "D04")}),
        ("unknown_account", {(("account_id",), "D06")}),
        ("negative", {(("amount",), "D05")}),
        ("late", {(("created_at",), "D10")}),
        ("bad_timestamp", {(("created_at",), "D04")}),
        ("renamed", {(("amount", "amt"), "D07")}),
        ("dropped", {(("status",), "D08")}),
    ],
)
def test_b2_reports_what_its_expectations_check(
    dataset: Path, suites: Path, tmp_path: Path, change: str, expected: set[Any]
) -> None:
    alerts = observe(B2Arm(suites), dataset, defective(dataset, tmp_path, change), tmp_path)
    assert expected <= claims(alerts), [a.message for a in alerts]
    assert all(a.arm == "B2" for a in alerts)


def test_b2_duplicate_keys_and_redelivered_batches(
    dataset: Path, suites: Path, tmp_path: Path
) -> None:
    duplicate = observe(B2Arm(suites), dataset, defective(dataset, tmp_path, "duplicate"), tmp_path)
    assert (("txn_id",), "D01") in claims(duplicate)
    again = observe(B2Arm(suites), dataset, defective(dataset, tmp_path, "previous"), tmp_path)
    assert {(("batch_id",), "D11"), (("created_at",), "D11")} <= claims(again)
    dimension = observe(B2Arm(suites), dataset, dataset / "transactions_raw" / "B000001.csv",
                        tmp_path, merchants_with_duplicate(dataset, tmp_path))  # fmt: skip
    assert (("merchant_id",), "D12") in claims(dimension)


def test_b2_describes_its_frozen_suites(suites: Path) -> None:
    described = B2Arm(suites).describe()
    assert described["tool"] == "great_expectations"
    assert set(described["suites"]) == {"transactions_raw.suite.json", "merchants.suite.json"}


# ---- wiring ----------------------------------------------------------------------------------


def test_the_cli_offers_every_arm_and_reports_effort() -> None:
    from rai_bench.cli import DEFAULT_CONTRACTS, _arms
    from rai_bench.runner import HarnessError

    contracts = Path(__file__).resolve().parents[2] / DEFAULT_CONTRACTS
    assert [a.name for a in _arms(["B1", "B2", "R"], contracts)] == ["B1", "B2", "R"]
    with pytest.raises(HarnessError, match="unknown arm"):
        _arms(["B3"], contracts)

    from rai_bench.baselines import effort

    counted = effort(contracts)
    assert counted["B1"]["checks"] == {"transactions_raw": 16, "fact_transaction": 20,
                                       "fraud_features": 24, "merchants": 7}  # fmt: skip
    # P1's 7 added, 1 replaced and 1 removed, plus P2's and P3's (B-28).
    assert counted["B2"]["hand_edits"] == {"added": 21, "replaced": 5, "removed": 4}
    assert counted["R"]["rules"]["transactions_raw"] == 36


def test_b2_profiles_development_data_only(dataset: Path, tmp_path: Path) -> None:
    import json

    copy = tmp_path / "eval"
    shutil.copytree(dataset, copy)
    manifest = json.loads((copy / "manifest.json").read_text("utf-8"))
    (copy / "manifest.json").write_text(json.dumps(manifest | {"seed_purpose": "evaluation"}))
    with pytest.raises(ValueError, match="development data only"):
        write_suites(copy, tmp_path / "suites")


def test_profile_and_effort_commands(tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from rai_bench.cli import app

    pytest.importorskip("great_expectations")
    runner = CliRunner()
    work, out = tmp_path / "dev", tmp_path / "suites"
    made = runner.invoke(app, ["b2-profile", "--work", str(work), "--out", str(out),
                               "--batches", "2", "--rows", "200"])  # fmt: skip
    assert made.exit_code == 0, made.output
    assert sorted(p.name for p in out.iterdir()) == [
        "merchants.profiled.json", "merchants.suite.json",
        "transactions_raw.profiled.json", "transactions_raw.suite.json",
    ]  # fmt: skip
    again = runner.invoke(app, ["b2-profile", "--work", str(work), "--out", str(out)])
    assert again.exit_code == 2
    assert "is not empty" in again.output
    contracts = Path(__file__).resolve().parents[2] / "contracts" / "sandboxbank"
    counted = runner.invoke(app, ["effort", "--contracts", str(contracts)])
    assert json.loads(counted.output)["B1"]["checks"]["transactions_raw"] == 16
