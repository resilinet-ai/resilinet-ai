"""rai-frame-digest/1: the content digest lineage records (B-19: FR-04.1)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from rai.connectors import connector
from rai.lineage.digest import DIGEST_FORMAT, frame_digest

FRAME = pl.DataFrame({"b": ["x", None], "a": ["1", "é"]})


def test_the_format_is_the_documented_document() -> None:
    """Pinned by hand, so any change to the encoding is a deliberate format change."""
    document = (
        'rai-frame-digest/1\n["a","b"]\n'
        '["1","x"]\n'
        '["é",null]\n'
    )  # fmt: skip
    expected = hashlib.sha256(document.encode("utf-8")).hexdigest()
    assert DIGEST_FORMAT == "rai-frame-digest/1"
    assert frame_digest(FRAME) == expected


def test_row_and_column_order_do_not_matter() -> None:
    shuffled = FRAME.select("a", "b").reverse()
    assert frame_digest(shuffled) == frame_digest(FRAME)


def test_any_change_to_the_content_changes_the_digest() -> None:
    original = frame_digest(FRAME)
    changed = [
        FRAME.with_columns(pl.Series("a", ["2", "é"])),  # a value
        FRAME.with_columns(pl.Series("b", ["x", ""])),  # null -> empty string
        FRAME.rename({"b": "c"}),  # a column name
        pl.concat([FRAME, FRAME.head(1)]),  # a duplicated row
        FRAME.head(1),  # a removed row
    ]
    digests = {frame_digest(frame) for frame in changed}
    assert original not in digests
    assert len(digests) == len(changed)


def test_an_empty_batch_has_a_digest_that_depends_on_its_columns() -> None:
    empty = FRAME.clear()
    assert frame_digest(empty) != frame_digest(empty.rename({"a": "z"}))


def test_the_same_data_has_the_same_digest_from_csv_and_parquet(tmp_path: Path) -> None:
    typed = pl.DataFrame(
        {
            "txn_id": ["T1", "T2"],
            "amount": ["12.50", None],
            "posted_at": [datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)],
        }
    )
    typed.write_parquet(tmp_path / "batch.parquet")
    (tmp_path / "batch.csv").write_text(
        "posted_at,amount,txn_id\n"
        "2026-01-02T00:00:00.000000Z,,T2\n"
        "2026-01-01T00:00:00.000000Z,12.50,T1\n",
        encoding="utf-8",
    )
    read = {
        kind: connector(
            {"name": "f", "environment": "SANDBOX-01", "type": kind, "root": str(tmp_path)}
        ).read(f"batch.{kind}", environment="SANDBOX-01")
        for kind in ("csv", "parquet")
    }
    assert frame_digest(read["csv"]) == frame_digest(read["parquet"])
    # Non-text frames are normalised the way connectors normalise them.
    assert frame_digest(typed) == frame_digest(read["parquet"])
