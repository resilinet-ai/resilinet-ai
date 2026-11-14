"""Defect injector and fixture sets (B-07, Protocol sections 4 and 8).

Each class is checked against the clean batch it came from: the ground truth must
name exactly the rows and columns that changed, nothing more and nothing less.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sandboxbank.cli import main
from sandboxbank.fixtures import build_fixtures, build_slice, plan, verify_seal
from sandboxbank.generator import (
    GeneratorConfig,
    generate_accounts,
    generate_batch,
    generate_merchants,
    stream,
)
from sandboxbank.injector import (
    FRESHNESS_WINDOW,
    RATES,
    Context,
    Injection,
    inject,
    instance_count,
)
from sandboxbank.schema import column_names

CONFIG = GeneratorConfig(seed=21, accounts=200, merchants=40, batches=2, rows_per_batch=2_000)
ACCOUNTS = generate_accounts(CONFIG)
MERCHANTS = generate_merchants(CONFIG)
HEADER = list(column_names("transactions_raw"))
M_HEADER = list(column_names("merchants"))
M_ROWS = [list(m.as_row()) for m in MERCHANTS]
PREVIOUS = [list(t.as_row()) for t in generate_batch(CONFIG, ACCOUNTS, MERCHANTS, 0)]
CLEAN = [list(t.as_row()) for t in generate_batch(CONFIG, ACCOUNTS, MERCHANTS, 1)]
LIMITS = {a.account_id: Decimal(a.txn_limit_cents).scaleb(-2) for a in ACCOUNTS}
CTX = Context(
    CONFIG.batch_start(1), HEADER, PREVIOUS, LIMITS, M_HEADER, M_ROWS, reemit_rows=PREVIOUS
)
COL = {name: i for i, name in enumerate(HEADER)}


def run(defect: str, rate: float | None, seed: int = 1) -> Injection:
    return inject(defect, stream(seed, f"test/{defect}"), HEADER, CLEAN, rate, CTX)


def changed_rows(result: Injection) -> list[int]:
    """Rows of the output that are not the clean row at the same position."""
    return [i for i, row in enumerate(result.rows) if row != CLEAN[i]]


# ---- row-level classes ----------------------------------------------------------


@pytest.mark.parametrize("rate", RATES["D01"])
def test_d01_inserts_exact_copies_with_the_same_txn_id(rate: float) -> None:
    result = run("D01", rate)
    expected = instance_count(rate, len(CLEAN))
    assert len(result.rows) == len(CLEAN) + expected
    assert len(result.rows_affected) == expected
    ids = Counter(row[COL["txn_id"]] for row in result.rows)
    for i in result.rows_affected:
        assert result.rows[i] in CLEAN
        assert ids[result.rows[i][COL["txn_id"]]] >= 2
    remaining = [row for i, row in enumerate(result.rows) if i not in set(result.rows_affected)]
    assert remaining == CLEAN
    assert result.columns == ["txn_id"]


def test_d02_re_emits_rows_from_the_previous_batch() -> None:
    result = run("D02", 0.01)
    previous_ids = {row[COL["txn_id"]] for row in PREVIOUS}
    assert len(result.rows_affected) == instance_count(0.01, len(CLEAN))
    assert all(result.rows[i] in PREVIOUS for i in result.rows_affected)
    assert set(result.keys) <= previous_ids
    remaining = [row for i, row in enumerate(result.rows) if i not in set(result.rows_affected)]
    assert remaining == CLEAN


@pytest.mark.parametrize(("defect", "column"), [("D04", "amount"), ("D06", "account_id")])
@pytest.mark.parametrize("rate", RATES["D04"])
def test_in_place_classes_change_exactly_the_reported_cells(
    defect: str, column: str, rate: float
) -> None:
    result = run(defect, rate)
    assert changed_rows(result) == result.rows_affected
    assert len(result.rows_affected) == instance_count(rate, len(CLEAN))
    assert result.columns == [column]
    for i in result.rows_affected:
        diff = [name for name in HEADER if result.rows[i][COL[name]] != CLEAN[i][COL[name]]]
        assert diff == [column]
        assert result.keys[result.rows_affected.index(i)] == CLEAN[i][COL["txn_id"]]


def test_d03_blanks_one_required_column() -> None:
    for seed in range(6):  # different seeds pick different required columns
        result = run("D03", 0.01, seed)
        (column,) = result.columns
        assert changed_rows(result) == result.rows_affected
        assert all(result.rows[i][COL[column]] == "" for i in result.rows_affected)
        assert result.keys == [CLEAN[i][COL["txn_id"]] for i in result.rows_affected]


def test_d04_values_are_not_numbers() -> None:
    result = run("D04", 0.01)
    for i in result.rows_affected:
        with pytest.raises(InvalidOperation):
            Decimal(result.rows[i][COL["amount"]])


def test_d05_amounts_are_negative_or_over_the_limit() -> None:
    result = run("D05", 0.01)
    assert changed_rows(result) == result.rows_affected
    kinds = result.detail["kind"]
    assert set(kinds.values()) == {"negative", "over_limit"}
    for i in result.rows_affected:
        amount = Decimal(result.rows[i][COL["amount"]])
        limit = LIMITS[result.rows[i][COL["account_id"]]]
        assert amount < 0 or amount > limit


def test_d06_references_accounts_that_do_not_exist() -> None:
    result = run("D06", 0.01)
    assert all(result.rows[i][COL["account_id"]] not in LIMITS for i in result.rows_affected)


@pytest.mark.parametrize("rate", RATES["D10"])
def test_d10_rows_fall_outside_the_freshness_window(rate: float) -> None:
    result = run("D10", rate)
    assert changed_rows(result) == result.rows_affected
    assert result.columns == ["created_at"]
    cutoff = CTX.window_start - FRESHNESS_WINDOW
    for i in result.rows_affected:
        stamp = datetime.fromisoformat(result.rows[i][COL["created_at"]])
        assert stamp < cutoff


# ---- whole-batch and dimension classes ------------------------------------------


def test_d07_renames_one_column_and_keeps_the_data() -> None:
    result = run("D07", None)
    assert result.whole_batch
    assert result.rows == CLEAN
    (old,) = result.columns
    assert old not in result.header
    assert result.detail["renamed_to"] in result.header


def test_d08_drops_one_column() -> None:
    result = run("D08", None)
    (dropped,) = result.columns
    assert result.header == [name for name in HEADER if name != dropped]
    assert all(len(row) == len(HEADER) - 1 for row in result.rows)


@pytest.mark.parametrize("seed", range(4))
def test_d09_changes_the_amount_representation(seed: int) -> None:
    result = run("D09", None, seed)
    assert result.columns == ["amount"]
    changed = sum(
        r[COL["amount"]] != c[COL["amount"]] for r, c in zip(result.rows, CLEAN, strict=True)
    )
    # Values exact in binary (12.25) print the same as floats, as a real float pipeline
    # would write them; the defect is the batch-wide type change, visible in most rows.
    assert changed > len(CLEAN) * 0.5
    if result.detail["became"] == "float":
        for r, c in zip(result.rows, CLEAN, strict=True):
            assert abs(float(r[COL["amount"]]) - float(c[COL["amount"]])) < 1e-9
    else:
        assert all(r[COL["amount"]].startswith("USD ") for r in result.rows)


def test_d11_redelivers_the_previous_batch() -> None:
    result = run("D11", None)
    assert result.whole_batch
    assert (result.header, result.rows) == (HEADER, PREVIOUS)


@pytest.mark.parametrize("rate", RATES["D12"])
def test_d12_duplicates_dimension_keys_that_the_batch_joins_to(rate: float) -> None:
    result = run("D12", rate)
    assert result.rows == CLEAN  # the batch itself is untouched
    assert result.merchants_rows is not None
    counts = Counter(row[0] for row in result.merchants_rows)
    duplicated = {key for key, n in counts.items() if n > 1}
    assert duplicated == set(result.keys)
    assert len(duplicated) == instance_count(rate, len(M_ROWS))
    assert result.rows_affected == [
        i for i, row in enumerate(CLEAN) if row[COL["merchant_id"]] in duplicated
    ]


def test_inject_rejects_bad_requests() -> None:
    rng = stream(1, "x")
    with pytest.raises(ValueError, match="unknown defect"):
        inject("D99", rng, HEADER, CLEAN, 0.01, CTX)
    with pytest.raises(ValueError, match="not a protocol rate"):
        inject("D01", rng, HEADER, CLEAN, 0.5, CTX)
    with pytest.raises(ValueError, match="empty batch"):
        inject("D01", rng, HEADER, [], 0.01, CTX)


def test_instance_count_is_never_zero() -> None:
    assert instance_count(0.0001, 2_000) == 1
    assert instance_count(0.01, 100_000) == 1_000
    assert instance_count(0.5, 1) == 1


@settings(max_examples=30, deadline=None)
@given(
    defect=st.sampled_from(["D01", "D02", "D03", "D04", "D05", "D06", "D10"]),
    seed=st.integers(min_value=0, max_value=99_999),
)
def test_ground_truth_rows_are_always_the_defective_ones(defect: str, seed: int) -> None:
    rate = RATES[defect][seed % len(RATES[defect])]
    assert rate is not None
    result = run(defect, rate, seed)
    flagged = set(result.rows_affected)
    clean_left = [row for i, row in enumerate(result.rows) if i not in flagged]
    assert clean_left == CLEAN or (
        defect not in {"D01", "D02"} and changed_rows(result) == sorted(flagged)
    )
    assert len(result.keys) == len(flagged)


# ---- fixture sets and the sealed answer key ---------------------------------------


def test_protocol_plan_shape() -> None:
    batches = plan(100_001)
    assert len(batches) == 180
    assert batches[0].defect_class is None
    by_class = Counter(b.defect_class for b in batches)
    assert by_class[None] == 36
    assert all(by_class[c] == 12 for c in RATES)
    rates = Counter((b.defect_class, b.rate) for b in batches if b.defect_class)
    assert rates[("D01", 0.0001)] == rates[("D01", 0.01)] == 4
    assert rates[("D10", 0.05)] == 6
    assert plan(100_001) == batches
    assert plan(100_002) != batches


def test_plan_rejects_empty_shapes() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        plan(1, clean=0)


SMALL = {"accounts": 80, "merchants": 20, "rows_per_batch": 150, "batches_per_class": 1, "clean": 2}


def test_fixture_set_is_reproducible_and_sealed(tmp_path: Path) -> None:
    first = build_fixtures(tmp_path / "a", data_seed=7, injector_seed=8, **SMALL)  # type: ignore[arg-type]
    second = build_fixtures(tmp_path / "b", data_seed=7, injector_seed=8, **SMALL)  # type: ignore[arg-type]
    assert first == second
    for path in first["files"]:
        assert (tmp_path / "a" / path).read_bytes() == (tmp_path / "b" / path).read_bytes()

    truth = [
        json.loads(line)
        for line in (tmp_path / "a" / "answer_key" / "ground_truth.jsonl")
        .read_text("utf-8")
        .splitlines()
    ]
    assert len(truth) == 14
    assert {t["defect_class"] for t in truth} == {None, *RATES}
    assert verify_seal(tmp_path / "a", first["answer_key_sha256"])
    assert "answer_key/ground_truth.jsonl" in first["files"]
    d12 = next(t for t in truth if t["defect_class"] == "D12")
    assert (tmp_path / "a" / d12["dimension_file"]).is_file()
    assert first["seed_purpose"] == "development"
    # Golden seal: CI on Linux must reproduce the answer key built on Windows exactly.
    assert first["answer_key_sha256"] == (
        "e5ba3fcc13ee2b937066408432f35fc99132cf2d40de83959130c13969453d5e"
    )


def test_tampered_answer_key_breaks_the_seal(tmp_path: Path) -> None:
    build_fixtures(tmp_path, data_seed=7, injector_seed=8, **SMALL)  # type: ignore[arg-type]
    key = tmp_path / "answer_key" / "ground_truth.jsonl"
    key.write_text(key.read_text("utf-8").replace('"D05"', '"D04"', 1), encoding="utf-8")
    assert not verify_seal(tmp_path)
    assert not verify_seal(tmp_path / "missing")


@pytest.mark.parametrize(
    ("data_seed", "injector_seed", "message"),
    [(7, 100_007, "same protocol range"), (7, 7, "must differ"), (250_000, 1, "outside")],
)
def test_fixture_seeds_are_checked(
    tmp_path: Path, data_seed: int, injector_seed: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build_fixtures(tmp_path, data_seed=data_seed, injector_seed=injector_seed, **SMALL)  # type: ignore[arg-type]


def test_fixtures_refuse_to_overwrite(tmp_path: Path) -> None:
    build_fixtures(tmp_path, data_seed=7, injector_seed=8, **SMALL)  # type: ignore[arg-type]
    with pytest.raises(FileExistsError):
        build_fixtures(tmp_path, data_seed=7, injector_seed=8, **SMALL)  # type: ignore[arg-type]


def test_cli_fixtures_and_seal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "fx"
    args = ["--data-seed", "3", "--injector-seed", "4", "--out", str(out), "--rows", "100"]
    small = ["--accounts", "50", "--merchants", "15", "--per-class", "1", "--clean", "1"]
    assert main(["fixtures", *args, *small]) == 0
    seal = capsys.readouterr().out.split("SEAL: ")[1].split()[0]
    assert main(["seal", str(out), "--expect", seal]) == 0
    assert main(["seal", str(out), "--expect", "0" * 64]) == 1
    assert main(["fixtures", *args, *small]) == 2  # not empty
    capsys.readouterr()


def test_d02_needs_earlier_delivered_rows() -> None:
    empty = Context(CTX.window_start, HEADER, PREVIOUS, LIMITS, M_HEADER, M_ROWS)
    with pytest.raises(ValueError, match="earlier delivered batch"):
        inject("D02", stream(1, "x"), HEADER, CLEAN, 0.01, empty)


def test_d02_and_d11_reuse_what_was_actually_delivered(tmp_path: Path) -> None:
    # Regression for a B-07 realism bug found in B-10: re-using the clean generated
    # batch could re-emit rows no arm ever received, making the defect undetectable.
    build_fixtures(
        tmp_path,
        data_seed=41,
        injector_seed=42,
        accounts=80,
        merchants=20,
        rows_per_batch=200,
        batches_per_class=4,
        clean=4,
    )
    truth = [
        json.loads(line)
        for line in (tmp_path / "answer_key" / "ground_truth.jsonl").read_text("utf-8").splitlines()
    ]
    delivered: list[tuple[str, list[str]]] = []  # (txn_id, raw line) of unaffected rows
    for index, batch in enumerate(truth):
        lines = (tmp_path / batch["file"]).read_text("utf-8").splitlines()
        if batch["defect_class"] == "D11":
            previous = (tmp_path / truth[index - 1]["file"]).read_bytes()
            assert (tmp_path / batch["file"]).read_bytes() == previous
        if batch["defect_class"] == "D02":
            earlier = {line for _, line in delivered}
            for row in batch["rows"]:
                assert lines[row + 1] in earlier, batch["batch_id"]
        if lines[0] == ",".join(HEADER) and batch["defect_class"] != "D09":
            flagged = set(batch["rows"])
            delivered += [
                (line.split(",")[0], line) for i, line in enumerate(lines[1:]) if i not in flagged
            ]


def test_slice_command_and_its_checks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "slice"
    args = ["slice", "--out", str(out), "--rows", "2000"]
    assert main(args) == 0
    assert "with 2 re-sent rows (D02)" in capsys.readouterr().out
    assert main(args) == 2  # not empty without --force
    assert main([*args, "--force"]) == 0
    assert (
        main(["slice", "--out", str(tmp_path / "x"), "--data-seed", "9", "--injector-seed", "9"])
        == 2
    )
    capsys.readouterr()
    with pytest.raises(ValueError, match="same protocol range"):
        build_slice(tmp_path / "y", data_seed=1, injector_seed=100_001)
