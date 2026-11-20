"""The synthetic canary privacy sentinel (FR-05.1): find canary values where they must
not be.

A ``privacy_canary`` test case reads its target asset (a warehouse table, an export)
and an answer key of *needles*: one row per value to look for, with the canary it
belongs to and its kind (``canary, kind, value``; SandboxBank's ``sandboxbank canaries
write`` writes ``needles.csv``). The canaries live in a source system's table that
nothing downstream should copy, so any needle in the target is a leak. Pure functions
over polars frames.

* **Search.** Every text column of the target (or the ``columns`` listed) is searched
  for every needle at once (Aho-Corasick), as a substring, ignoring ASCII case: a
  leaked email inside a free-text note, or upper-cased by a transform, still counts.
* **Never repeat a needle.** A hit names the canary, the kind of value, the column and
  the 0-based row, with the value masked (``av…id, 31 chars``). Nothing this module
  returns holds a needle in full, so observations, errors and logs cannot leak it.
* **A key that cannot work is an error, not a pass.** No needles, a needle shorter than
  ``MIN_NEEDLE`` characters (it would match by chance), a value listed twice, or a
  listed column missing from the target: the step is an error.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_NEEDLE = 6
KEY_COLUMNS = ("canary", "kind", "value")
MAX_SAMPLE = 50


class CanaryKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    asset: str = Field(min_length=1, max_length=200)  # an asset of the same project
    connector: str = Field(min_length=1, max_length=100)


class CanaryDefinition(BaseModel):
    """What a ``privacy_canary`` test case's ``definition`` holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connector: str = Field(min_length=1, max_length=100)  # reads the target asset
    environment: str
    key: CanaryKey
    columns: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=500)
    case_sensitive: bool = False

    @field_validator("columns")
    @classmethod
    def _unique(cls, columns: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if columns is not None and len(set(columns)) != len(columns):
            raise ValueError("a column is listed twice")
        return columns


@dataclass(frozen=True)
class Needle:
    canary: str
    kind: str
    value: str


@dataclass(frozen=True)
class Hit:
    column: str
    row: int
    canary: str
    kind: str
    masked: str

    def as_dict(self) -> dict[str, object]:
        return {"column": self.column, "row": self.row, "canary": self.canary,
                "kind": self.kind, "value": self.masked}  # fmt: skip


@dataclass(frozen=True)
class Scan:
    rows: int
    columns: tuple[str, ...]
    needles: int
    canaries: int
    hits: int = 0
    hit_rows: int = 0
    sample: list[Hit] = field(default_factory=list)
    by_kind: dict[str, int] = field(default_factory=dict)
    by_column: dict[str, int] = field(default_factory=dict)
    by_canary: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def outcome(self) -> str:
        return "error" if self.error else ("fail" if self.hits else "pass")

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.hits:
            return f"no canary value in {self.rows:,} rows of {len(self.columns)} column(s)"
        first = ", ".join(
            f"{h.kind} of {h.canary} in {h.column!r} row {h.row} ({h.masked})"
            for h in self.sample[:3]
        )
        return (f"{self.hits} canary value(s) of {len(self.by_canary)} canary(ies) found in "
                f"{self.hit_rows} row(s): {first}")  # fmt: skip


def mask(value: str) -> str:
    """Enough to recognise a value by, never enough to recover it."""
    if len(value) < 8:
        return f"•••, {len(value)} chars"
    return f"{value[:2]}…{value[-2:]}, {len(value)} chars"


def needles(key: pl.DataFrame) -> tuple[list[Needle], str | None]:
    """The needles of an answer key, or why the key cannot be used."""
    missing = [c for c in KEY_COLUMNS if c not in key.columns]
    if missing:
        return [], f"the canary key has no {', '.join(map(repr, missing))} column"
    rows = key.select(pl.col(c).cast(pl.String) for c in KEY_COLUMNS).drop_nulls()
    found = [Needle(*row) for row in rows.iter_rows()]
    if not found:
        return [], "the canary key lists no needles"
    short = [n for n in found if len(n.value) < MIN_NEEDLE]
    if short:
        first = f"{short[0].kind} of {short[0].canary}"
        return [], (f"{len(short)} needle(s) are shorter than {MIN_NEEDLE} characters and "
                    f"would match by chance (first: {first})")  # fmt: skip
    repeated = [v for v, n in Counter(n.value.lower() for n in found).items() if n > 1]
    if repeated:
        return [], f"{len(repeated)} needle value(s) are listed twice"
    return found, None


def _outermost(values: list[str], starts: list[int]) -> list[str]:
    """Matches not inside a longer one: a leaked email is reported as the email, not
    also as the marker it contains. Same-span matches (one value) count once."""
    spans = sorted({(s, s + len(v), v) for v, s in zip(values, starts, strict=True)},
                   key=lambda span: (span[0], -span[1]))  # fmt: skip
    kept: list[tuple[int, int, str]] = []
    for start, end, value in spans:
        if not any(a <= start and end <= b for a, b, _ in kept):
            kept.append((start, end, value))
    return [value for _, _, value in kept]


def scan(target: pl.DataFrame, found: list[Needle], definition: CanaryDefinition) -> Scan:
    """Every needle in every searched column of the target, as masked hits."""
    columns = definition.columns or tuple(
        name for name, dtype in target.schema.items() if dtype == pl.String
    ) or tuple(target.columns)  # fmt: skip
    base = Scan(target.height, tuple(columns), len(found), len({n.canary for n in found}))
    missing = [c for c in columns if c not in target.columns]
    if missing:
        return replace(base, error=f"column(s) {', '.join(map(repr, missing))} not in the target")
    fold = (lambda v: v) if definition.case_sensitive else str.lower
    lookup = {fold(n.value): n for n in found}
    patterns = [n.value for n in found]
    hits: list[Hit] = []
    rows_hit: set[int] = set()
    by_kind: Counter[str] = Counter()
    by_column: Counter[str] = Counter()
    by_canary: Counter[str] = Counter()
    total = 0
    for column in columns:
        text = pl.col(column).cast(pl.String)
        insensitive = not definition.case_sensitive
        matched = target.select(
            pl.int_range(pl.len()).alias("row"),
            text.str.extract_many(patterns, ascii_case_insensitive=insensitive,
                                  overlapping=True).alias("found"),
            text.str.find_many(patterns, ascii_case_insensitive=insensitive,
                               overlapping=True).alias("at"),
        ).filter(pl.col("found").list.len() > 0)  # fmt: skip
        for row, values, starts in matched.iter_rows():
            rows_hit.add(row)
            for value in _outermost(values, starts):
                needle = lookup[fold(value)]
                total += 1
                by_kind[needle.kind] += 1
                by_column[column] += 1
                by_canary[needle.canary] += 1
                if len(hits) < MAX_SAMPLE:
                    hits.append(Hit(column, row, needle.canary, needle.kind, mask(needle.value)))
    return Scan(
        base.rows, base.columns, base.needles, base.canaries, hits=total,
        hit_rows=len(rows_hit), sample=hits, by_kind=dict(by_kind),
        by_column=dict(by_column), by_canary=dict(by_canary),
    )  # fmt: skip
