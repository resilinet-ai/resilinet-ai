"""Post-recovery consistency checks (FR-08.3). Pure functions over polars frames.

A ``consistency_check`` reconciles a **target** asset (the test's asset, e.g. the
warehouse's ``fact_transaction``) against a **source** it is fed from (a declared
upstream asset, e.g. the ledger). Placed after a chaos scenario in a plan, it shows
whether the system came back *correct*, not just healthy: SandboxBank's ETL promises
that a load stopped or restarted mid-way resumes "with nothing lost or doubled".

The source keeps growing while the target catches up, so the check takes a
**snapshot** of the source when it starts and then, round by round, reads the
target and a fresh copy of the source:

* ``consistency.missing``: snapshot keys not in the target (lost);
* ``consistency.duplicated``: keys the target holds more than once (doubled);
* ``consistency.unexpected``: target keys the source does not have (phantoms);
* ``consistency.mismatched``: keys whose compared columns differ.

It passes when all four are empty; the worker retries until they are or the window
closes. With ``allow_unexpected``, target rows the source never had are still counted
but do not fail the check: the target may also be fed by other writers (a ledger
reconciled against one seeder's log). Rows written after the snapshot are not
required in the target yet, but they must still match if they are there. Values
are compared as the connectors return them (text), so both sides must render a
column the same way.

**RPO ground truth (B-30; threat T-07).** With ``rpo``, the source is SandboxBank's
seeding log: every write the payments-api *acknowledged*, recorded by the seeder
outside the system under test. Acknowledged writes missing from the ledger are
broken promises, whatever the ledger's own head says. The ground-truth RPO is the
fault's onset minus the earliest lost acknowledgement (0 s if nothing was lost, and
never negative); any lost acknowledged write fails the step. The worker also sets it
beside the ledger-based RPO of the same run (``recovery.rpo``, B-26b) and fails the
step if the two disagree on whether anything was lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

SAMPLE = 10
RULES = ("missing", "duplicated", "unexpected", "mismatched")


class Source(BaseModel):
    """The upstream asset the target is reconciled against."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    asset: str = Field(min_length=1, max_length=200)  # an upstream asset of the target, by name
    connector: str = Field(min_length=1, max_length=100)
    # Equality filters on text values, e.g. {"status": "posted"}: what the target holds.
    where: dict[str, str] = Field(default_factory=dict, max_length=10)
    # Source column -> target column, where the two names differ.
    rename: dict[str, str] = Field(default_factory=dict, max_length=20)


class RpoTruth(BaseModel):
    """RPO from the seeding log (B-30): which source column holds the acknowledgement time."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    at: str = Field(default="acked_at", min_length=1, max_length=63)
    objective_seconds: int | None = Field(default=None, ge=0, le=86_400)


class ConsistencyDefinition(BaseModel):
    """What a ``consistency_check`` test case's ``definition`` holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connector: str = Field(min_length=1, max_length=100)  # reads the target asset
    environment: str
    source: Source
    key: tuple[str, ...] = Field(min_length=1, max_length=5)
    compare: tuple[str, ...] = Field(default=(), max_length=20)
    where: dict[str, str] = Field(default_factory=dict, max_length=10)  # on the target
    within_seconds: int = Field(default=300, ge=0, le=3600)
    interval_seconds: float = Field(default=10.0, ge=0.01, le=300)
    allow_unexpected: bool = False  # the target has other writers too: count, do not fail
    rpo: RpoTruth | None = None  # B-30: the source is a seeding log of acknowledged writes

    @field_validator("key", "compare")
    @classmethod
    def _columns(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(names)) != len(names):
            raise ValueError("a column is listed twice")
        return names


@dataclass(frozen=True)
class Finding:
    rule: str
    count: int
    sample: tuple[dict[str, Any], ...]  # up to SAMPLE keys
    required: bool = True  # False: counted and reported, but it does not fail the check

    @property
    def passed(self) -> bool:
        return self.count == 0

    @property
    def outcome(self) -> str:
        return "pass" if self.passed else ("fail" if self.required else "skipped")


@dataclass(frozen=True)
class Reconciliation:
    findings: tuple[Finding, ...]
    snapshot_rows: int
    source_rows: int
    target_rows: int

    @property
    def passed(self) -> bool:
        return all(f.passed or not f.required for f in self.findings)

    def summary(self) -> str:
        failed = [f"{f.count} {f.rule}" for f in self.findings if f.outcome == "fail"]
        return ", ".join(failed) if failed else "consistent"


def prepare_source(frame: pl.DataFrame, definition: ConsistencyDefinition) -> pl.DataFrame:
    """The source as the target should hold it: filtered, then renamed."""
    filtered = _filter(frame, definition.source.where, "source")
    missing = sorted(set(definition.source.rename) - set(filtered.columns))
    if missing:
        raise ValueError(f"source has no column {', '.join(missing)} to rename")
    return filtered.rename(definition.source.rename)


def prepare_target(frame: pl.DataFrame, definition: ConsistencyDefinition) -> pl.DataFrame:
    return _filter(frame, definition.where, "target")


def reconcile(
    snapshot: pl.DataFrame,
    source: pl.DataFrame,
    target: pl.DataFrame,
    *,
    key: tuple[str, ...],
    compare: tuple[str, ...] = (),
    allow_unexpected: bool = False,
) -> Reconciliation:
    """Frames already prepared (:func:`prepare_source`, :func:`prepare_target`)."""
    columns = [*key, *compare]
    for side, frame in (("source", source), ("target", target), ("source", snapshot)):
        absent = [c for c in columns if c not in frame.columns]
        if absent:
            raise ValueError(f"{side} has no column {', '.join(absent)}")
    keys = list(key)
    counted = target.group_by(keys).len()
    duplicated = counted.filter(pl.col("len") > 1).drop("len").sort(keys)
    target_keys = counted.drop("len")
    missing = snapshot.select(keys).unique().join(target_keys, on=keys, how="anti").sort(keys)
    fresh = source.select(columns).unique(subset=keys, keep="first")
    unexpected = target_keys.join(fresh.select(keys), on=keys, how="anti").sort(keys)
    mismatched = _mismatched(target.select(columns), fresh, keys, list(compare))
    findings = tuple(
        Finding(rule, frame.height, tuple(frame.head(SAMPLE).to_dicts()),
                required=not (allow_unexpected and rule == "unexpected"))
        for rule, frame in zip(RULES, (missing, duplicated, unexpected, mismatched), strict=True)
    )  # fmt: skip
    return Reconciliation(findings, snapshot.height, source.height, target.height)


def _mismatched(
    target: pl.DataFrame, source: pl.DataFrame, keys: list[str], compare: list[str]
) -> pl.DataFrame:
    if not compare:
        return target.select(keys).clear()
    joined = target.join(source, on=keys, how="inner", suffix="__source")
    differs = pl.any_horizontal([pl.col(c).ne_missing(pl.col(f"{c}__source")) for c in compare])
    return joined.filter(differs).select(keys).unique().sort(keys)


def _filter(frame: pl.DataFrame, where: dict[str, str], side: str) -> pl.DataFrame:
    absent = sorted(set(where) - set(frame.columns))
    if absent:
        raise ValueError(f"{side} has no column {', '.join(absent)} to filter on")
    for column, value in where.items():
        frame = frame.filter(pl.col(column).cast(pl.Utf8) == value)
    return frame


@dataclass(frozen=True)
class GroundTruth:
    """RPO from acknowledged writes the target no longer holds (B-30)."""

    lost: int
    earliest_lost_at: datetime | None
    onset: datetime | None
    seconds: float | None  # None: something was lost but there was no fault onset to date it
    sample: tuple[dict[str, Any], ...]


def ground_truth(
    snapshot: pl.DataFrame,
    target: pl.DataFrame,
    *,
    key: tuple[str, ...],
    at: str,
    onset: datetime | None,
) -> GroundTruth:
    """``snapshot``: the acknowledged writes (prepared source); ``target``: the ledger."""
    if at not in snapshot.columns:
        raise ValueError(f"source has no column {at} (the acknowledgement time)")
    keys = list(key)
    lost = (
        snapshot.unique(subset=keys, keep="first")
        .join(target.select(keys).unique(), on=keys, how="anti")
        .sort(at)
    )
    if lost.height == 0:
        return GroundTruth(0, None, onset, 0.0, ())
    times = [_instant(value) for value in lost.get_column(at).to_list()]
    earliest = min(times)
    seconds = None if onset is None else max(0.0, (onset - earliest).total_seconds())
    sample = tuple(lost.select([*keys, at]).head(SAMPLE).to_dicts())
    return GroundTruth(lost.height, earliest, onset, seconds, sample)


def _instant(value: Any) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO 8601 acknowledgement time") from None
    if moment.tzinfo is None:
        raise ValueError(f"{value!r} has no time zone")
    return moment
