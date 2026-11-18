"""Post-recovery consistency: reconciling a target against its source (FR-08.3)."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from rai.recovery.consistency import (
    ConsistencyDefinition,
    prepare_source,
    prepare_target,
    reconcile,
)

DEFINITION = ConsistencyDefinition.model_validate({
    "connector": "warehouse", "environment": "SANDBOX-01",
    "source": {"asset": "ledger_transactions", "connector": "ledger",
               "where": {"status": "posted"}, "rename": {"seq": "ledger_seq"}},
    "key": ["txn_id"], "compare": ["amount", "ledger_seq"],
})  # fmt: skip


def ledger(*rows: tuple[str, str, str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        [dict(zip(("seq", "txn_id", "amount", "status"), r, strict=True)) for r in rows],
        schema={"seq": pl.Utf8, "txn_id": pl.Utf8, "amount": pl.Utf8, "status": pl.Utf8},
    )


def facts(*rows: tuple[str, str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        [dict(zip(("ledger_seq", "txn_id", "amount"), r, strict=True)) for r in rows],
        schema={"ledger_seq": pl.Utf8, "txn_id": pl.Utf8, "amount": pl.Utf8},
    )


LEDGER = ledger(("1", "a", "10.00", "posted"), ("2", "b", "20.00", "posted"),
                ("3", "c", "5.00", "declined"))  # fmt: skip


def check(target: pl.DataFrame, source: pl.DataFrame = LEDGER,
          snapshot: pl.DataFrame = LEDGER) -> dict[str, Any]:  # fmt: skip
    result = reconcile(
        prepare_source(snapshot, DEFINITION), prepare_source(source, DEFINITION),
        prepare_target(target, DEFINITION), key=DEFINITION.key, compare=DEFINITION.compare,
    )  # fmt: skip
    return {f.rule: (f.count, list(f.sample)) for f in result.findings} | {
        "passed": result.passed,
        "summary": result.summary(),
    }


def test_a_target_that_holds_exactly_the_posted_source_rows_is_consistent() -> None:
    seen = check(facts(("1", "a", "10.00"), ("2", "b", "20.00")))
    assert (seen["passed"], seen["summary"]) == (True, "consistent")
    assert seen["missing"] == (0, [])  # the declined row is not expected in the target


def test_lost_doubled_phantom_and_changed_rows_are_each_found() -> None:
    seen = check(facts(("2", "b", "20.00"), ("2", "b", "20.00"), ("9", "z", "1.00")))
    assert seen["missing"] == (1, [{"txn_id": "a"}])
    assert seen["duplicated"] == (1, [{"txn_id": "b"}])
    assert seen["unexpected"] == (1, [{"txn_id": "z"}])
    assert seen["mismatched"][0] == 0
    changed = check(facts(("1", "a", "10.01"), ("2", "b", "20.00")))
    assert changed["mismatched"] == (1, [{"txn_id": "a"}])
    assert changed["summary"] == "1 mismatched"


def test_rows_written_after_the_snapshot_need_not_have_arrived_yet() -> None:
    later = LEDGER.vstack(ledger(("4", "d", "7.00", "posted")))
    assert check(facts(("1", "a", "10.00"), ("2", "b", "20.00")), source=later)["passed"]
    arrived_wrong = check(facts(("1", "a", "10.00"), ("2", "b", "20.00"), ("4", "d", "7.50")),
                          source=later)  # fmt: skip
    assert arrived_wrong["mismatched"] == (1, [{"txn_id": "d"}])  # but must match once there


def test_samples_are_capped() -> None:
    many = ledger(*[(str(n), f"t{n:03}", "1.00", "posted") for n in range(25)])
    seen = check(facts(), source=many, snapshot=many)
    assert seen["missing"][0] == 25
    assert len(seen["missing"][1]) == 10


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"key": ["nope"]}, "source has no column nope"),
        ({"source": {"asset": "l", "connector": "c", "where": {"x": "1"}}},
         "no column x to filter"),
        ({"source": {"asset": "l", "connector": "c", "rename": {"x": "y"}}},
         "no column x to rename"),
    ],
)  # fmt: skip
def test_columns_that_do_not_exist_are_reported(change: dict[str, Any], message: str) -> None:
    definition = ConsistencyDefinition.model_validate(DEFINITION.model_dump() | change)
    with pytest.raises(ValueError, match=message):
        reconcile(prepare_source(LEDGER, definition), prepare_source(LEDGER, definition),
                  prepare_target(facts(), definition), key=definition.key,
                  compare=definition.compare)  # fmt: skip


@pytest.mark.parametrize(
    "change",
    [{"key": []}, {"key": ["a", "a"]}, {"within_seconds": 3601}, {"interval_seconds": 0},
     {"command": "drop"}, {"source": {"asset": "l"}}],
)  # fmt: skip
def test_definitions_are_checked(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ConsistencyDefinition.model_validate(DEFINITION.model_dump() | change)
