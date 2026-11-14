"""Polars rule engine (B-09: FR-03.1, NFR-PERF-1).

The acceptance test scores the engine against a sealed SandboxBank fixture set:
for every in-scope defect class the failing rows must equal the answer key
exactly, and clean batches must raise nothing.
"""

from __future__ import annotations

import json
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from rai.validation.contract import Contract, load_contract, parse_contract
from rai.validation.engine import (
    DEFERRED,
    SAMPLE_LIMIT,
    failing_rows,
    read_batch,
    validate,
)
from rai.validation.history import MemoryKeyStore
from sandboxbank.fixtures import build_fixtures
from sandboxbank.generator import (
    GeneratorConfig,
    generate_accounts,
    generate_batch,
    generate_merchants,
)
from sandboxbank.schema import column_names

ROOT = Path(__file__).resolve().parents[2]
TXN_CONTRACT = load_contract(ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml")


def contract(columns: str, extra: str = "") -> Contract:
    return parse_contract(f"contract: t\nversion: 1\n{extra}\ncolumns:\n{columns}")


def failing(c: Contract, frame: pl.DataFrame, rule_id: str, **kwargs: Any) -> list[int]:
    return failing_rows(c, frame, rule_id, **kwargs)


# ---- type rules ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column_type", "good", "bad"),
    [
        ("uuid", ["196c1a05-f633-49ec-a32d-c3901c963dda"], ["196c1a05", "not-a-uuid"]),
        ("integer", ["0", "-12", " 7 "], ["1.5", "1e3", "x"]),
        ('"decimal(6,2)"', ["0.5", "1234.56", "-3"], ["12345.6", "1.234", "1,234.50", "USD 1.00"]),
        ('"decimal(4,0)"', ["1234"], ["1.5", "12345"]),
        ("float", ["1.5", "1e3", "-0.25"], ["one", "1,5"]),
        ("boolean", ["true", "FALSE"], ["yes", "1"]),
        ("date", ["2026-01-31"], ["2026-02-30", "31/01/2026"]),
        (
            "timestamp",
            ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00.123Z", "2026-01-01T02:00:00+02:00"],
            ["2026-01-01 00:00:00", "2026-01-01T00:00:00", "2026-13-01T00:00:00Z"],
        ),
        ("string", ["anything", "123"], []),
    ],
)
def test_type_rule(column_type: str, good: list[str], bad: list[str]) -> None:
    c = contract(f"  v: {{ type: {column_type} }}")
    frame = pl.DataFrame({"v": [*good, *bad, "", None]})
    assert failing(c, frame, "v.type") == list(range(len(good), len(good) + len(bad)))


def test_required_counts_null_and_blank() -> None:
    c = contract("  v: { type: string, required: true }")
    assert failing(c, pl.DataFrame({"v": ["a", "", "  ", None, "b"]}), "v.required") == [1, 2, 3]


# ---- value rules ignore missing and malformed values -----------------------------------


def test_pattern_and_allowed() -> None:
    c = contract("  v: { type: string, pattern: '^A[0-9]$', allowed: [A1, A2] }")
    frame = pl.DataFrame({"v": ["A1", "A3", "B1", "", None, " A2 "]})
    assert failing(c, frame, "v.pattern") == [2, 5]  # leading space breaks the anchored pattern
    assert failing(c, frame, "v.allowed") == [1, 2]


@pytest.mark.parametrize(
    ("spec", "values", "expected"),
    [
        ("{ type: integer, min: 0, max: 10 }", ["-1", "0", "10", "11", "x", ""], [0, 3]),
        ('{ type: "decimal(8,2)", min: 0.01 }', ["0.00", "0.01", "-5.00", "abc"], [0, 2]),
        ("{ type: float, max: 1.5 }", ["1.5", "1.50001", "nan?"], [1]),
        (
            "{ type: date, min: '2026-01-01', max: '2026-12-31' }",
            ["2025-12-31", "2026-06-01", "2027-01-01", "bad"],
            [0, 2],
        ),
        (
            "{ type: timestamp, min: '2026-01-01T00:00:00Z' }",
            ["2025-12-31T23:59:59Z", "2026-01-01T01:00:00+01:00", "2026-01-01T00:00:01Z"],
            [0],
        ),
    ],
)
def test_range(spec: str, values: list[str], expected: list[int]) -> None:
    c = contract(f"  v: {spec}")
    assert failing(c, pl.DataFrame({"v": values}), "v.range") == expected


ACCOUNTS = pl.DataFrame({"account_id": ["A1", "A2"], "txn_limit": ["100.00", "5000.00"]})
LOOKUP = contract(
    "  account_id: { type: string, references: accounts.account_id }\n"
    '  amount: { type: "decimal(10,2)", max_lookup: { ref: accounts.txn_limit, key: account_id } }'
)


def test_references_and_max_lookup() -> None:
    frame = pl.DataFrame(
        {
            "account_id": ["A1", "A2", "A9", "", "A1"],
            "amount": ["100.00", "100.01", "999999.00", "1.00", "100.01"],
        }
    )
    refs = {"accounts": ACCOUNTS}
    assert failing(LOOKUP, frame, "account_id.references", references=refs) == [2]
    # A9 has no limit to compare with: references reports it, max_lookup does not.
    assert failing(LOOKUP, frame, "amount.max_lookup", references=refs) == [4]
    assert failing(LOOKUP, frame.reverse(), "amount.max_lookup", references=refs) == [0]


def test_missing_reference_data_is_an_error_not_a_pass() -> None:
    frame = pl.DataFrame({"account_id": ["A1"], "amount": ["1.00"]})
    results = validate(LOOKUP, frame).by_rule()
    assert results["account_id.references"].outcome == "error"
    assert "accounts.account_id was not supplied" in results["account_id.references"].message
    assert results["amount.max_lookup"].outcome == "error"
    no_key = validate(LOOKUP, frame.drop("account_id"), references={"accounts": ACCOUNTS})
    assert "'account_id' is not in the batch" in no_key.by_rule()["amount.max_lookup"].message


# ---- freshness -------------------------------------------------------------------------


FRESH = contract("  ts: { type: timestamp }", "freshness: { column: ts, max_lag: 1h }")


def test_freshness_against_arrival_time() -> None:
    frame = pl.DataFrame({"ts": ["2026-01-01T09:30:00Z", "2026-01-01T08:59:59Z", "bad", ""]})
    as_of = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert failing(FRESH, frame, "freshness", as_of=as_of) == [1]


def test_freshness_falls_back_to_the_newest_timestamp() -> None:
    frame = pl.DataFrame({"ts": ["2026-01-01T10:00:00Z", "2026-01-01T08:00:00Z"]})
    report = validate(FRESH, frame)
    assert report.by_rule()["freshness"].failing_count == 1
    assert report.notes == ("freshness measured against the newest timestamp in the batch",)
    empty = validate(FRESH, pl.DataFrame({"ts": ["bad"]}))
    assert empty.by_rule()["freshness"].outcome == "error"


def test_date_freshness() -> None:
    c = contract("  d: { type: date }", "freshness: { column: d, max_lag: 2d }")
    frame = pl.DataFrame({"d": ["2026-01-10", "2026-01-07"]})
    assert failing(c, frame, "freshness", as_of=datetime(2026, 1, 10, tzinfo=UTC)) == [1]


# ---- report shape ------------------------------------------------------------------------


def test_thresholds_samples_and_deferred_rules() -> None:
    c = contract(
        '  id: { type: integer, unique: true }\n  v: { type: integer, threshold: "10%" }',
        "unique_across_batches: [id]",
    )
    frame = pl.DataFrame({"id": [str(i) for i in range(30)], "v": ["x"] * 25 + ["1"] * 5})
    results = validate(c, frame).by_rule()
    assert results["v.type"].outcome == "fail"
    assert results["v.type"].failing_count == 25
    assert results["v.type"].sample_rows == tuple(range(SAMPLE_LIMIT))
    assert results["v.type"].threshold == "10%"
    assert results["id.type"].outcome == "pass"
    assert results["id.unique"].outcome == "pass"
    assert results["schema"].outcome == "skipped"
    assert results["schema"].message == DEFERRED["schema"]
    across = results["unique_across_batches:id"]
    assert (across.outcome, across.message) == (
        "error",
        "no batch history supplied for cross-batch duplicates",
    )
    within = validate(c, frame.with_columns(pl.lit("1").alias("v")).head(10)).by_rule()
    assert within["v.type"].outcome == "pass"


def test_missing_columns_and_non_text_input() -> None:
    c = contract("  a: { type: integer, required: true }\n  b: { type: string, required: true }")
    report = validate(c, pl.DataFrame({"a": [1, 2, None]}))  # ints are judged as their text
    results = report.by_rule()
    assert results["a.type"].outcome == "pass"
    assert results["a.required"].failing_count == 1
    assert results["b.required"].outcome == "error"
    assert results["b.required"].message == "column 'b' is not in the batch"
    assert report.with_outcome("error") == [results["b.required"], results["b.type"]]
    assert report.contract_ref == "t@1"


def test_failing_rows_lookup_errors() -> None:
    frame = pl.DataFrame({"a": ["1"]})
    c = contract("  a: { type: integer, unique: true }")
    with pytest.raises(KeyError):
        failing_rows(c, frame, "nope")
    assert failing_rows(c, frame, "a.unique") == []


# ---- acceptance: the engine against a sealed SandboxBank fixture set ---------------------


ROW_LEVEL = {
    "D02": ["unique_across_batches:txn_id"],
    "D04": ["amount.type"],
    "D05": ["amount.range", "amount.max_lookup"],
    "D06": ["account_id.references"],
    "D10": ["freshness"],
}
MERCHANTS_CONTRACT = load_contract(ROOT / "contracts" / "sandboxbank" / "merchants.yaml")


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("fixtures")
    build_fixtures(
        out,
        data_seed=31,
        injector_seed=32,
        accounts=300,
        merchants=60,
        rows_per_batch=3_000,
        batches_per_class=3,
        clean=6,
    )
    return out


def test_engine_scores_exactly_against_the_answer_key(fixtures: Path) -> None:
    """Replay the feed in delivery order, as a pipeline would, and score every batch."""
    refs = {
        "accounts": read_batch(fixtures / "feed" / "accounts.csv"),
        "merchants": read_batch(fixtures / "feed" / "merchants.csv"),
    }
    truth = [
        json.loads(line)
        for line in (fixtures / "answer_key" / "ground_truth.jsonl").read_text("utf-8").splitlines()
    ]
    history = MemoryKeyStore()
    seen = set()
    for batch in truth:
        frame = read_batch(fixtures / batch["file"])
        as_of = datetime.fromisoformat(batch["onset"])
        kwargs: dict[str, Any] = {"references": refs, "as_of": as_of, "history": history}
        kwargs["batch_id"] = batch["batch_id"]
        report = validate(TXN_CONTRACT, frame, record=True, **kwargs)
        raised = {r.rule_id for r in report.results if r.outcome in {"fail", "error"}}
        defect = batch["defect_class"]
        seen.add(defect)
        context = f"{batch['batch_id']} {defect}"
        if defect in ROW_LEVEL:
            found: set[int] = set()
            for rule_id in ROW_LEVEL[defect]:
                found |= set(failing_rows(TXN_CONTRACT, frame, rule_id, **kwargs))
            assert sorted(found) == batch["rows"], context
            assert raised <= set(ROW_LEVEL[defect]), context
        elif defect == "D01":
            # Copies land before or after their original; either way each copy is one
            # extra occurrence, so the duplicated keys and the count match exactly.
            rows = failing_rows(TXN_CONTRACT, frame, "txn_id.unique")
            keys = frame.get_column("txn_id").gather(rows).to_list()
            assert sorted(keys) == sorted(batch["keys"]), context
            assert raised == {"txn_id.unique"}, context
        elif defect == "D03":
            rule_id = f"{batch['columns'][0]}.required"
            assert failing_rows(TXN_CONTRACT, frame, rule_id) == batch["rows"], context
            # A blank txn_id or created_at is not also a type or duplicate failure.
            assert raised == {rule_id}, context
        elif defect == "D09":
            assert raised == {"amount.type"}, context
            assert report.by_rule()["amount.type"].failing_count > frame.height / 2, context
        elif defect in {"D07", "D08"}:
            missing = batch["columns"][0]
            # Rules on the column, and rules that need it as a lookup key, report an error.
            assert f"{missing}.type" in raised, context
            for rule in raised:
                result = report.by_rule()[rule]
                assert result.outcome == "error", context
                assert result.message == f"column {missing!r} is not in the batch", context
        elif defect == "D11":
            # Every row of a re-delivered batch was delivered before (it may also carry
            # the earlier batch's own defects, which are already in the answer key).
            across = report.by_rule()["unique_across_batches:txn_id"]
            with_key = frame.filter(pl.col("txn_id").str.strip_chars() != "").height
            assert across.outcome == "fail", context
            assert across.failing_count == with_key, context
        elif defect == "D12":
            assert raised == set(), context  # the batch is clean; the dimension is not
            dimension = read_batch(fixtures / batch["dimension_file"])
            dim_report = validate(MERCHANTS_CONTRACT, dimension).by_rule()
            dim_raised = {k for k, r in dim_report.items() if r.outcome in {"fail", "error"}}
            assert dim_raised == {"merchant_id.unique"}, context
            rows = failing_rows(MERCHANTS_CONTRACT, dimension, "merchant_id.unique")
            dup_keys = sorted(dimension.get_column("merchant_id").gather(rows).to_list())
            assert dup_keys == batch["keys"], context
        else:  # clean
            assert raised == set(), context
    assert seen == {None, *(f"D{n:02d}" for n in range(1, 13))}
    clean_dimension = validate(MERCHANTS_CONTRACT, refs["merchants"])
    assert clean_dimension.with_outcome("fail") == []


# ---- NFR-PERF-1: a 100k-row batch against the 36-rule contract in under 10 s --------------


def test_100k_rows_validate_well_under_ten_seconds() -> None:
    config = GeneratorConfig(seed=99, rows_per_batch=100_000)
    accounts, merchants = generate_accounts(config), generate_merchants(config)
    header = list(column_names("transactions_raw"))
    rows = [t.as_row() for t in generate_batch(config, accounts, merchants, 0)]
    frame = pl.DataFrame(rows, schema=header, orient="row")
    refs = {
        "accounts": pl.DataFrame(
            [a.as_row() for a in accounts], schema=list(column_names("accounts")), orient="row"
        ),
        "merchants": pl.DataFrame(
            [m.as_row() for m in merchants], schema=list(column_names("merchants")), orient="row"
        ),
    }
    started = time.perf_counter()
    report = validate(
        TXN_CONTRACT,
        frame,
        references=refs,
        as_of=config.batch_start(1),
        history=MemoryKeyStore(),
        batch_id="B000000",
        record=True,
    )
    elapsed = time.perf_counter() - started
    assert report.row_count == 100_000
    assert report.with_outcome("fail") == []
    assert report.with_outcome("error") == []
    assert elapsed < 10, f"{elapsed:.2f}s"


def test_validation_raises_no_deprecation_warnings() -> None:
    # Polars emits some deprecations from its Rust core, where warning filters cannot turn
    # them into errors, so record them instead: a deprecated call breaks on the next major.
    frame = pl.DataFrame({"account_id": ["A1", "A9"], "amount": ["1.00", "2.00"]})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate(LOOKUP, frame, references={"accounts": ACCOUNTS})
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations == [], [str(w.message) for w in deprecations]
