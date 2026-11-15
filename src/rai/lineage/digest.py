"""Content digests of the data a run actually read (B-19: FR-04.1).

``rai-frame-digest/1`` is the SHA-256 of this UTF-8 document:

    rai-frame-digest/1
    ["<column>", ...]          column names, sorted
    ["<value>" | null, ...]    one line per row: values in sorted-column order,
    ...                        lines sorted by their UTF-8 bytes

Each JSON line uses ``,`` and ``:`` without spaces and keeps non-ASCII characters
as they are (Python's ``json.dumps(..., ensure_ascii=False, separators=(",", ":"))``);
every line ends with ``\\n``.

The digest is taken over the frame **after** the connector has normalised it to
text (see :mod:`rai.connectors.base`), so the same data has the same digest
whether it was read from CSV, Parquet or PostgreSQL. It is independent of row and
column order, because a PostgreSQL read has no defined row order; it does count
duplicate rows, and tells a null from an empty string. Changing any value, adding
or removing a row, or renaming a column changes it.
"""

from __future__ import annotations

import hashlib
import json

import polars as pl

from rai.connectors.base import to_text

DIGEST_FORMAT = "rai-frame-digest/1"


def _line(values: object) -> bytes:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def frame_digest(frame: pl.DataFrame) -> str:
    """The ``rai-frame-digest/1`` SHA-256 of a batch, as 64 lowercase hex digits."""
    if any(dtype != pl.Utf8 for dtype in frame.schema.values()):
        frame = to_text(frame)
    columns = sorted(frame.columns)
    rows = sorted(_line(list(row)) for row in frame.select(columns).iter_rows())
    digest = hashlib.sha256()
    digest.update(DIGEST_FORMAT.encode("utf-8") + b"\n")
    digest.update(_line(columns) + b"\n")
    for row in rows:
        digest.update(row + b"\n")
    return digest.hexdigest()
