"""Comparison arms (Protocol section 3) and the RAI arm adapter (B-15).

An arm sees only what a real pipeline would deliver: the batch file, the
reference tables in ``feed/``, the dimension sent with the batch (if any) and the
batch's arrival time. It never sees the answer key. Each arm turns its own
findings into common-format alerts (``rai_bench.alerts``).

The baselines B1 (conventional pytest suite) and B2 (Great Expectations) are
written and frozen on days 45-50 under the protocol's fairness rules; they plug in
through the same ``Arm`` interface.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from rai import __version__ as rai_version
from rai.validation.contract import Contract, load_contract
from rai.validation.drift import SchemaChange
from rai.validation.engine import RuleResult, ValidationReport, read_batch, validate
from rai.validation.history import SqliteKeyStore
from rai_bench.alerts import Alert

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class Delivery:
    """One batch as the pipeline delivers it."""

    batch_id: str
    file: Path
    feed: Path
    arrival: datetime  # end of the batch's window on SandboxBank's clock
    dimension: Path | None = None  # merchants table sent with this batch, if any


class Arm(Protocol):
    name: str

    def describe(self) -> dict[str, Any]:
        """Version and configuration hashes, recorded in run.json."""
        ...

    def open(self, workdir: Path, feed: Path) -> None:
        """Prepare for a run; ``workdir`` is the arm's own state folder."""
        ...

    def observe(self, delivery: Delivery, clock: Clock) -> list[Alert]:
        """Check one delivered batch; return the alerts raised."""
        ...

    def close(self) -> None: ...


# Which catalogue class each RAI finding claims (fixed, written from the contracts'
# rule kinds and the defect catalogue, never from evaluation fixtures).
_BATCH_CLASS = {
    "unique": "D01",
    "primary_key": "D01",
    "required": "D03",
    "type": "D04",
    "range": "D05",
    "max_lookup": "D05",
    "references": "D06",
}
_SCHEMA_CLASS = {"renamed": "D07", "removed": "D08", "type_change": "D09"}


def claimed_class(result: RuleResult, *, dimension: bool) -> str | None:
    """The RAI arm's defect-class claim for a failing or erroring rule."""
    if result.outcome != "fail":
        return None  # a rule that could not run says nothing about the class
    if dimension:
        return "D12" if result.kind in ("unique", "primary_key") else None
    whole_batch = result.row_count > 0 and result.failing_count == result.row_count
    if result.kind == "unique_across_batches":
        return "D11" if whole_batch else "D02"  # every row seen before: re-delivered batch
    if result.kind == "freshness":
        return "D11" if whole_batch else "D10"
    return _BATCH_CLASS.get(result.kind)


def _schema_location(change: SchemaChange) -> tuple[str, ...]:
    return (change.column, change.new_name) if change.new_name else (change.column,)


def report_alerts(
    report: ValidationReport, batch_id: str, moment: datetime, *, dimension: bool
) -> list[Alert]:
    """One alert per rule that did not pass; a failing schema rule gives one alert per
    breaking change, so each change names its own column."""
    prefix = "merchants dimension: " if dimension else ""
    alerts: list[Alert] = []
    for result in report.results:
        if result.outcome == "pass":
            continue
        if result.kind == "schema":
            for change in report.schema_changes:
                if change.impact != "breaking":
                    continue
                alerts.append(
                    Alert(
                        arm="R",
                        subject_kind="batch",
                        subject_id=batch_id,
                        time=moment,
                        location=_schema_location(change),
                        message=f"{prefix}{result.rule_id}: {change.describe()}",
                        defect_class=_SCHEMA_CLASS.get(change.kind),
                    )
                )
            continue
        counted = (
            f"{result.failing_count} of {result.row_count} rows fail"
            if result.outcome == "fail"
            else result.message
        )
        alerts.append(
            Alert(
                arm="R",
                subject_kind="batch",
                subject_id=batch_id,
                time=moment,
                location=result.columns,
                message=f"{prefix}{result.rule_id} ({result.severity}): {counted}",
                defect_class=claimed_class(result, dimension=dimension),
            )
        )
    return alerts


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RaiArm:
    """Arm R: RAI's data contracts through the validation engine, with a persistent
    cross-batch key history, exactly as ``rai validate`` runs them."""

    name = "R"

    def __init__(self, contracts: Path) -> None:
        self._batch_path = contracts / "transactions_raw.yaml"
        self._dimension_path = contracts / "merchants.yaml"
        self._batch_contract: Contract = load_contract(self._batch_path)
        self._dimension_contract: Contract = load_contract(self._dimension_path)
        self._references: dict[str, pl.DataFrame] = {}
        self._history: SqliteKeyStore | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "arm": self.name,
            "tool": "rai",
            "version": rai_version,
            "contracts": {
                self._batch_contract.ref: _sha256(self._batch_path),
                self._dimension_contract.ref: _sha256(self._dimension_path),
            },
        }

    def open(self, workdir: Path, feed: Path) -> None:
        self._references = {
            "accounts": read_batch(feed / "accounts.csv"),
            "merchants": read_batch(feed / "merchants.csv"),
        }
        self._history = SqliteKeyStore(workdir / "history.sqlite")

    def observe(self, delivery: Delivery, clock: Clock) -> list[Alert]:
        if self._history is None:
            raise RuntimeError("open() the arm before observing batches")
        report = validate(
            self._batch_contract,
            read_batch(delivery.file),
            references=self._references,
            as_of=delivery.arrival,
            history=self._history,
            batch_id=delivery.batch_id,
            record=True,
        )
        alerts = report_alerts(report, delivery.batch_id, clock(), dimension=False)
        if delivery.dimension is not None:
            dimension = validate(self._dimension_contract, read_batch(delivery.dimension))
            alerts += report_alerts(dimension, delivery.batch_id, clock(), dimension=True)
        return alerts

    def close(self) -> None:
        if self._history is not None:
            self._history.close()
            self._history = None
