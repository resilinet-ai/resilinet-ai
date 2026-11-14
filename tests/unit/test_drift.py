"""Schema drift detection and classification (B-11, FR-03.2).

Spec acceptance: a renamed column is flagged breaking. The fixture-based check
(D07, D08, D09 against the answer key) is in test_engine.py.
"""

from __future__ import annotations

import polars as pl
import pytest

from rai.validation.contract import parse_contract
from rai.validation.drift import TYPE_CHANGE_SHARE, SchemaChange, detect_drift, infer_type
from rai.validation.engine import validate

BASE = """
contract: payments
version: 1
columns:
  txn_id: { type: uuid }
  account_id: { type: string }
  amount: { type: "decimal(14,2)" }
  created_at: { type: timestamp }
"""
CONTRACT = parse_contract(BASE)
UUID = "196c1a05-f633-49ec-a32d-c3901c963dda"


def batch(**columns: list[str]) -> pl.DataFrame:
    base = {
        "txn_id": [UUID, UUID],
        "account_id": ["ACC-0000001", "ACC-0000002"],
        "amount": ["10.00", "2.50"],
        "created_at": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"],
    }
    return pl.DataFrame(base | columns)


def kinds(changes: list[SchemaChange]) -> list[tuple[str, str, str | None, str]]:
    return [(c.kind, c.column, c.new_name, c.impact) for c in changes]


def test_a_matching_batch_has_no_drift() -> None:
    assert detect_drift(CONTRACT, batch()) == []
    reordered = batch().select("created_at", "amount", "account_id", "txn_id")
    assert detect_drift(CONTRACT, reordered) == []  # order is not a schema change


def test_renamed_column_is_breaking() -> None:
    frame = batch().rename({"account_id": "acct_id"})
    changes = detect_drift(CONTRACT, frame)
    assert kinds(changes) == [("renamed", "account_id", "acct_id", "breaking")]
    result = validate(CONTRACT, frame).by_rule()["schema"]
    assert result.outcome == "fail"
    assert result.message == "column 'account_id' renamed to 'acct_id' (breaking)"


def test_two_renames_pair_by_name_and_values() -> None:
    frame = batch().rename({"amount": "amt", "created_at": "created_ts"})
    assert kinds(detect_drift(CONTRACT, frame)) == [
        ("renamed", "amount", "amt", "breaking"),
        ("renamed", "created_at", "created_ts", "breaking"),
    ]


def test_values_that_do_not_fit_are_not_a_rename() -> None:
    # 'amount' vanished and 'note' appeared, but 'note' holds text: removed + added.
    frame = batch(note=["hello", "world"]).drop("amount")
    assert kinds(detect_drift(CONTRACT, frame)) == [
        ("removed", "amount", None, "breaking"),
        ("added", "note", None, "non_breaking"),
    ]


def test_dissimilar_names_do_not_pair_when_several_columns_change() -> None:
    frame = (
        batch()
        .drop("amount", "created_at")
        .with_columns(
            pl.lit("5.00").alias("zz"), pl.lit("2026-01-01T00:00:00Z").alias("created_ts")
        )
    )
    assert kinds(detect_drift(CONTRACT, frame)) == [
        ("removed", "amount", None, "breaking"),
        ("renamed", "created_at", "created_ts", "breaking"),
        ("added", "zz", None, "non_breaking"),
    ]


def test_added_column_is_non_breaking_by_default() -> None:
    frame = batch(channel=["p2p", "ach"])
    result = validate(CONTRACT, frame).by_rule()["schema"]
    assert result.outcome == "pass"
    assert result.message == "column 'channel' added (non-breaking)"


def test_policy_decides_the_impact() -> None:
    strict = parse_contract(
        BASE
        + "schema_change_policy:\n"
        + "  added_column: breaking\n"
        + "  removed_or_renamed_column: non_breaking\n"
    )
    assert validate(strict, batch(extra=["1", "2"])).by_rule()["schema"].outcome == "fail"
    lenient = validate(strict, batch().drop("account_id")).by_rule()["schema"]
    assert (lenient.outcome, lenient.message) == (
        "pass",
        "column 'account_id' removed (non-breaking)",
    )


@pytest.mark.parametrize(
    ("amounts", "observed"),
    [
        (["89.739999999999995", "2.5"], "decimal(17,15)"),
        (["USD 1,234.50", "USD 2.00"], "string"),
        (["1e3", "2.5e-1"], "float"),
        (["12", "7"], None),  # integers still fit decimal(14,2): no change
    ],
)
def test_type_change_reports_the_observed_type(amounts: list[str], observed: str | None) -> None:
    changes = detect_drift(CONTRACT, batch(amount=amounts))
    if observed is None:
        assert changes == []
    else:
        (change,) = changes
        assert (change.kind, change.expected_type, change.observed_type, change.impact) == (
            "type_change",
            "decimal(14,2)",
            observed,
            "breaking",
        )
        assert change.describe() == f"column 'amount' type decimal(14,2) -> {observed} (breaking)"


def test_a_few_bad_values_are_value_errors_not_a_type_change() -> None:
    assert TYPE_CHANGE_SHARE == 0.5
    under = ["1.00"] * 51 + ["x"] * 49  # 49% bad: the type rule's job
    at = ["1.00"] * 50 + ["x"] * 50  # 50% bad: the column's type changed
    frame = pl.DataFrame({"txn_id": [UUID] * 100, "account_id": ["A"] * 100})
    created = ["2026-01-01T00:00:00Z"] * 100
    assert (
        detect_drift(
            CONTRACT, frame.with_columns(amount=pl.Series(under), created_at=pl.Series(created))
        )
        == []
    )
    (change,) = detect_drift(
        CONTRACT, frame.with_columns(amount=pl.Series(at), created_at=pl.Series(created))
    )
    assert change.kind == "type_change"


def test_missing_values_are_ignored_for_type_change() -> None:
    assert detect_drift(CONTRACT, batch(amount=["", "1.00"])) == []
    assert detect_drift(CONTRACT, batch(amount=["", ""])) == []


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["true", "False"], "boolean"),
        (["12", "-3"], "integer"),
        (["1.5", "-22.75", "3"], "decimal(4,2)"),
        (["2026-01-01T00:00:00Z"], "timestamp"),
        (["2026-01-01"], "date"),
        ([UUID], "uuid"),
        (["1e3"], "float"),
        (["hello", "1"], "string"),
        (["", None], "unknown"),
    ],
)
def test_infer_type(values: list[str | None], expected: str) -> None:
    assert infer_type(pl.Series(values, dtype=pl.Utf8)) == expected
