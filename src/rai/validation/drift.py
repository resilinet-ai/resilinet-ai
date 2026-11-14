"""Schema drift between a contract version and a delivered batch (B-11, FR-03.2).

Four kinds of change are reported, each classified by the contract's
``schema_change_policy`` as breaking or non-breaking:

* **added**: a column the contract does not declare (``added_column``).
* **removed**: a declared column missing from the batch (``removed_or_renamed_column``).
* **renamed**: a removed column paired with an added one whose values fit the
  removed column's type (at least 95% of present values) and whose name is
  similar. One removed plus one added column always pair if the values fit.
* **type_change**: at least half of a column's present values no longer fit its
  declared type (``type_change``). The observed type is reported, for example
  ``decimal(14,2) -> decimal(32,15)``. A few bad values are value errors for the
  ``type`` rule, not a schema change, so the two never compete for one defect.

Column order is not a schema change: batches are matched by column name.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

import polars as pl

from rai.validation.contract import ChangeImpact, ColumnType, Contract, parse_type
from rai.validation.values import missing, valid

ChangeKind = Literal["added", "removed", "renamed", "type_change"]

RENAME_FIT = 0.95
RENAME_NAME_SIMILARITY = 0.4
TYPE_CHANGE_SHARE = 0.5

# Tried in this order; decimal (with its observed precision and scale) comes before float,
# because every decimal string is also a valid float.
_INFERENCE_ORDER: tuple[str, ...] = ("boolean", "integer", "timestamp", "date", "uuid")


@dataclass(frozen=True, slots=True)
class SchemaChange:
    kind: ChangeKind
    column: str
    impact: ChangeImpact
    new_name: str | None = None
    expected_type: str | None = None
    observed_type: str | None = None

    def describe(self) -> str:
        match self.kind:
            case "added":
                text = f"column {self.column!r} added"
            case "removed":
                text = f"column {self.column!r} removed"
            case "renamed":
                text = f"column {self.column!r} renamed to {self.new_name!r}"
            case "type_change":
                text = f"column {self.column!r} type {self.expected_type} -> {self.observed_type}"
        return f"{text} ({self.impact.replace('_', '-')})"


def _share_valid(frame: pl.DataFrame, column: str, column_type: ColumnType) -> tuple[int, int]:
    """(present values, present values that fit the type)."""
    present = ~missing(column)
    row = frame.select(
        present.sum().alias("present"), (present & valid(column, column_type)).sum().alias("ok")
    ).row(0)
    return int(row[0]), int(row[1])


def infer_type(values: pl.Series) -> str:
    """The narrowest contract type that every present value fits ("string" otherwise)."""
    frame = pl.DataFrame({"v": values.cast(pl.Utf8)}).filter(~missing("v"))
    if frame.height == 0:
        return "unknown"
    text = pl.col("v").str.strip_chars()
    for candidate in _INFERENCE_ORDER:
        if frame.select(valid("v", parse_type(candidate)).all()).item():
            return candidate
    decimal = frame.select(text.str.contains(r"^-?[0-9]+(\.[0-9]+)?$").all()).item()
    if decimal:
        parts = frame.select(
            text.str.replace("^-", "").str.split(".").list.get(0).str.len_chars().max().alias("w"),
            text.str.split(".").list.get(1, null_on_oob=True).str.len_chars().max().alias("s"),
        ).row(0)
        whole, scale = int(parts[0]), int(parts[1] or 0)
        return f"decimal({whole + scale},{scale})"
    if frame.select(valid("v", parse_type("float")).all()).item():
        return "float"
    return "string"


def detect_drift(contract: Contract, batch: pl.DataFrame) -> list[SchemaChange]:
    """Every schema change between ``contract`` and ``batch`` (all columns as text)."""
    policy = contract.schema_change_policy
    frame = batch.with_columns(pl.all().cast(pl.Utf8))
    declared = list(contract.columns)
    removed = [c for c in declared if c not in frame.columns]
    added = [c for c in frame.columns if c not in contract.columns]

    changes: list[SchemaChange] = []
    unmatched = list(added)
    for column in removed:
        column_type = contract.columns[column].column_type
        best: tuple[float, str] | None = None
        for candidate in unmatched:
            present, ok = _share_valid(frame, candidate, column_type)
            if present and ok / present < RENAME_FIT:
                continue
            score = SequenceMatcher(None, column, candidate).ratio()
            one_to_one = len(removed) == 1 and len(added) == 1
            if (score >= RENAME_NAME_SIMILARITY or one_to_one) and (
                best is None or score > best[0]
            ):
                best = (score, candidate)
        if best is None:
            changes.append(SchemaChange("removed", column, policy.removed_or_renamed_column))
        else:
            unmatched.remove(best[1])
            changes.append(
                SchemaChange("renamed", column, policy.removed_or_renamed_column, new_name=best[1])
            )
    changes += [SchemaChange("added", column, policy.added_column) for column in unmatched]

    for column in declared:
        if column not in frame.columns:
            continue
        column_type = contract.columns[column].column_type
        if column_type.base == "string":
            continue
        present, ok = _share_valid(frame, column, column_type)
        if present and (present - ok) / present >= TYPE_CHANGE_SHARE:
            changes.append(
                SchemaChange(
                    "type_change",
                    column,
                    policy.type_change,
                    expected_type=str(column_type),
                    observed_type=infer_type(frame.get_column(column)),
                )
            )
    return changes
