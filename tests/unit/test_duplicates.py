"""Duplicate detection within and across batches (B-10, FR-03.3).

Acceptance (spec): a seeded duplicate is detected, and the output is repeatable.
The end-to-end check against the sealed fixtures lives in test_engine.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest

from rai.validation.contract import parse_contract
from rai.validation.engine import failing_rows, history_scope, validate
from rai.validation.history import KeyStore, MemoryKeyStore, SqliteKeyStore

CONTRACT = parse_contract(
    """
contract: payments
version: 1
primary_key: [id]
unique_across_batches: [id]
columns:
  id: { type: string }
  v:  { type: string }
"""
)
COMPOSITE = parse_contract(
    """
contract: positions
version: 1
primary_key: [account, day]
unique_across_batches: [account, day]
columns:
  account: { type: string }
  day: { type: date }
"""
)
ACROSS = "unique_across_batches:id"


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[KeyStore]:
    if request.param == "memory":
        yield MemoryKeyStore()
    else:
        with SqliteKeyStore(tmp_path / "history" / "keys.sqlite") as sqlite:
            yield sqlite


def frame(*ids: str | None) -> pl.DataFrame:
    return pl.DataFrame({"id": list(ids), "v": ["x"] * len(ids)})


# ---- within a batch ---------------------------------------------------------------


def test_every_occurrence_after_the_first_is_flagged() -> None:
    batch = frame("a", "b", "a", "c", "a", "b", "", None, "")
    assert failing_rows(CONTRACT, batch, "id.unique") == [2, 4, 5]  # blanks: required's job
    result = validate(CONTRACT, batch).by_rule()["id.unique"]
    assert (result.outcome, result.failing_count) == ("fail", 3)


def test_keys_are_compared_after_trimming() -> None:
    assert failing_rows(CONTRACT, frame("a", " a "), "id.unique") == [1]


def test_composite_primary_key() -> None:
    batch = pl.DataFrame(
        {
            "account": ["A", "A", "B", "A", "A"],
            "day": ["2026-01-01", "2026-01-02", "2026-01-01", "2026-01-01", ""],
        }
    )
    assert failing_rows(COMPOSITE, batch, "primary_key") == [3]


def test_separator_cannot_make_different_keys_collide() -> None:
    batch = pl.DataFrame({"account": ["A-B", "A"], "day": ["C", "B-C"]})
    assert failing_rows(COMPOSITE, batch, "primary_key") == []


# ---- across batches -------------------------------------------------------------------


def test_cross_batch_duplicates_and_repeatable_output(store: KeyStore) -> None:
    validate(CONTRACT, frame("a", "b", "c"), history=store, batch_id="B1", record=True)
    second = frame("d", "b", "e", "c", "b")
    first_look = validate(CONTRACT, second, history=store, batch_id="B2", record=True)
    again = validate(CONTRACT, second, history=store, batch_id="B2", record=True)
    for report in (first_look, again):  # re-validating B2 gives the same answer
        across = report.by_rule()[ACROSS]
        assert (across.outcome, across.failing_count, across.sample_rows) == ("fail", 3, (1, 3, 4))
    assert failing_rows(CONTRACT, second, ACROSS, history=store, batch_id="B2") == [1, 3, 4]
    # The first batch is never a duplicate of itself, even after later batches were recorded.
    assert (
        validate(CONTRACT, frame("a", "b", "c"), history=store, batch_id="B1")
        .by_rule()[ACROSS]
        .outcome
        == "pass"
    )


def test_looking_does_not_record(store: KeyStore) -> None:
    validate(CONTRACT, frame("a"), history=store, batch_id="B1")  # record defaults to False
    failing_rows(CONTRACT, frame("b"), ACROSS, history=store, batch_id="B1")
    assert (
        validate(CONTRACT, frame("a", "b"), history=store, batch_id="B2")
        .by_rule()[ACROSS]
        .failing_count
        == 0
    )


def test_first_sighting_is_kept(store: KeyStore) -> None:
    scope = history_scope(CONTRACT, next(r for r in CONTRACT.rules() if r.id == ACROSS))
    assert store.record(scope, ["a", "b", "b"], "B1") == 2
    assert store.record(scope, ["b", "c"], "B2") == 1
    assert store.seen_elsewhere(scope, ["a", "b", "c", "z"], "B2") == {"a", "b"}
    assert store.seen_elsewhere(scope, ["a", "b", "c"], None) == {"a", "b", "c"}
    assert store.seen_elsewhere("other:scope", ["a"], None) == set()


def test_composite_keys_across_batches(store: KeyStore) -> None:
    day1 = pl.DataFrame({"account": ["A", "B"], "day": ["2026-01-01", "2026-01-01"]})
    day2 = pl.DataFrame({"account": ["A", "A"], "day": ["2026-01-01", "2026-01-02"]})
    validate(COMPOSITE, day1, history=store, batch_id="D1", record=True)
    rule = "unique_across_batches:account+day"
    assert failing_rows(COMPOSITE, day2, rule, history=store, batch_id="D2") == [0]


def test_history_persists_across_reopening(tmp_path: Path) -> None:
    path = tmp_path / "keys.sqlite"
    with SqliteKeyStore(path) as first:
        validate(CONTRACT, frame("a", "b"), history=first, batch_id="B1", record=True)
    with SqliteKeyStore(path) as reopened:
        report = validate(CONTRACT, frame("b", "c"), history=reopened, batch_id="B2")
    assert report.by_rule()[ACROSS].sample_rows == (0,)


def test_history_is_shared_across_contract_versions(store: KeyStore) -> None:
    v2 = parse_contract(CONTRACT.canonical_json().replace('"version":1', '"version":2'))
    validate(CONTRACT, frame("a"), history=store, batch_id="B1", record=True)
    assert (
        validate(v2, frame("a"), history=store, batch_id="B2").by_rule()[ACROSS].failing_count == 1
    )


def test_recording_needs_history_and_batch_id() -> None:
    with pytest.raises(ValueError, match="record=True needs"):
        validate(CONTRACT, frame("a"), record=True)
    with pytest.raises(ValueError, match="record=True needs"):
        validate(CONTRACT, frame("a"), history=MemoryKeyStore(), record=True)
