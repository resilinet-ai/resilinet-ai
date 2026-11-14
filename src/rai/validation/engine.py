"""Polars rule engine (B-09, FR-03.1): evaluates a contract's rules on one batch.

Batches are read with every column as text, exactly as delivered, so a value that
does not fit its declared type is a finding (``amount.type``) rather than a crash
while loading. Each rule becomes one vectorised boolean expression, "this row
fails", and all of them are evaluated together.

Rule semantics:

* **missing** means null or blank. ``required`` counts missing values; every
  other value rule ignores missing values, so one problem is reported once.
* ``range``, ``max_lookup`` and ``freshness`` judge only values that pass
  ``type``; malformed values are already counted by the ``type`` rule.
* ``references`` and ``max_lookup`` need the reference table (for example
  ``accounts``); without it the rule reports ``error``, never ``pass``.
* ``freshness``: a row is late if its timestamp is older than ``as_of`` minus
  ``max_lag``. ``as_of`` is the batch arrival time when known, otherwise the
  newest timestamp in the batch.
* A rule on a column that is missing from the batch reports ``error``.
* ``unique`` and ``primary_key`` (B-10, FR-03.3) flag every occurrence of a key
  *after the first*, so the failing count is the number of extra copies.
  Rows with a missing key part are left to ``required``.
* ``unique_across_batches`` flags rows whose key a *different* batch delivered
  earlier, using a key history (:mod:`rai.validation.history`). Validating does
  not change the history unless ``record=True``; without a history the rule
  reports ``error``.

* ``schema`` (B-11, FR-03.2) is batch-level: it fails when the batch has a
  breaking schema change under the contract's policy (:mod:`rai.validation.drift`)
  and lists every change, breaking or not, in its message.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from rai.validation.contract import Contract, Rule, Severity
from rai.validation.drift import SchemaChange, detect_drift
from rai.validation.history import KeyStore
from rai.validation.values import bound as _bound
from rai.validation.values import missing as _missing
from rai.validation.values import typed as _typed
from rai.validation.values import valid as _valid

Outcome = Literal["pass", "fail", "error", "skipped"]
SAMPLE_LIMIT = 20
ROW = "__rai_row"

KEY_SEPARATOR = "\x1f"  # joins composite key parts; never appears in delivered text


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    kind: str
    columns: tuple[str, ...]
    severity: Severity
    outcome: Outcome
    row_count: int
    failing_count: int = 0
    threshold: str = "0"
    sample_rows: tuple[int, ...] = ()  # 0-based data rows of the batch, first 20
    message: str = ""


@dataclass(frozen=True, slots=True)
class ValidationReport:
    contract_ref: str
    contract_sha256: str
    row_count: int
    results: tuple[RuleResult, ...]
    duration_seconds: float
    as_of: datetime | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    schema_changes: tuple[SchemaChange, ...] = field(default_factory=tuple)

    def by_rule(self) -> dict[str, RuleResult]:
        return {result.rule_id: result for result in self.results}

    def with_outcome(self, outcome: Outcome) -> list[RuleResult]:
        return [result for result in self.results if result.outcome == outcome]


def read_batch(path: Path) -> pl.DataFrame:
    """Read a CSV batch with every column as text, as delivered."""
    return pl.read_csv(path, infer_schema=False)


# ---- evaluation ------------------------------------------------------------------------


@dataclass(slots=True)
class _Plan:
    """Per-rule failure expressions, plus the columns joined in for lookups."""

    failures: dict[str, pl.Expr] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)  # rule id -> error message
    frame: pl.DataFrame = field(default_factory=pl.DataFrame)
    keys_to_record: dict[str, list[str]] = field(default_factory=dict)  # scope -> keys


@dataclass(frozen=True, slots=True)
class _Inputs:
    references: Mapping[str, pl.DataFrame]
    as_of: datetime | None
    history: KeyStore | None
    batch_id: str | None


def history_scope(contract: Contract, rule: Rule) -> str:
    """History is kept per asset and rule, not per version, so it survives new versions."""
    return f"{contract.contract}:{rule.id}"


def _key(columns: tuple[str, ...]) -> tuple[pl.Expr, pl.Expr]:
    """The normalised key of each row, and whether every part of it is present."""
    parts = [pl.col(c).str.strip_chars() for c in columns]
    present = pl.all_horizontal([~_missing(c) for c in columns])
    key = parts[0] if len(parts) == 1 else pl.concat_str(parts, separator=KEY_SEPARATOR)
    return key, present


def _plan(
    contract: Contract,
    rules: list[Rule],
    batch: pl.DataFrame,
    inputs: _Inputs,
) -> tuple[_Plan, list[str]]:
    plan = _Plan(frame=batch.with_row_index(ROW))
    notes: list[str] = []
    references, as_of = inputs.references, inputs.as_of
    for rule in rules:
        if rule.kind == "schema":
            continue  # batch-level: evaluated in validate()
        missing_columns = [c for c in rule.columns if c not in batch.columns]
        if missing_columns:
            plan.problems[rule.id] = f"column {missing_columns[0]!r} is not in the batch"
            continue
        if rule.kind in {"unique", "primary_key"}:
            key, present_key = _key(rule.columns)
            plan.failures[rule.id] = present_key & ~key.is_first_distinct()
            continue
        if rule.kind == "unique_across_batches":
            if inputs.history is None:
                plan.problems[rule.id] = "no batch history supplied for cross-batch duplicates"
                continue
            key, present_key = _key(rule.columns)
            keys = plan.frame.filter(present_key).select(key.unique()).to_series().to_list()
            scope = history_scope(contract, rule)
            seen = inputs.history.seen_elsewhere(scope, keys, inputs.batch_id)
            plan.keys_to_record[scope] = keys
            plan.failures[rule.id] = present_key & key.is_in(
                pl.Series(sorted(seen), dtype=pl.Utf8).implode()
            )
            continue
        name = rule.columns[0]
        column_type = contract.columns[name].column_type
        present = ~_missing(name)
        match rule.kind:
            case "required":
                plan.failures[rule.id] = _missing(name)
            case "type":
                plan.failures[rule.id] = present & ~_valid(name, column_type)
            case "pattern":
                plan.failures[rule.id] = present & ~pl.col(name).str.contains(
                    rule.params["pattern"]
                )
            case "allowed":
                allowed = [str(value) for value in rule.params["values"]]
                plan.failures[rule.id] = present & ~pl.col(name).str.strip_chars().is_in(allowed)
            case "range":
                value = _typed(name, column_type)
                fails = pl.lit(value=False)
                if rule.params.get("min") is not None:
                    fails = fails | (value < _bound(rule.params["min"], column_type))
                if rule.params.get("max") is not None:
                    fails = fails | (value > _bound(rule.params["max"], column_type))
                plan.failures[rule.id] = fails.fill_null(value=False)
            case "references":
                table = references.get(rule.params["table"])
                if table is None or rule.params["column"] not in table.columns:
                    plan.problems[rule.id] = (
                        f"reference table {rule.params['table']}.{rule.params['column']} "
                        "was not supplied"
                    )
                    continue
                known = table.get_column(rule.params["column"]).str.strip_chars().unique()
                plan.failures[rule.id] = present & ~pl.col(name).str.strip_chars().is_in(
                    known.implode()
                )
            case "max_lookup":
                table = references.get(rule.params["table"])
                key, column = rule.params["key"], rule.params["column"]
                if table is None or column not in table.columns or key not in table.columns:
                    plan.problems[rule.id] = (
                        f"reference table {rule.params['table']}.{column} was not supplied"
                    )
                    continue
                if key not in plan.frame.columns:
                    plan.problems[rule.id] = f"column {key!r} is not in the batch"
                    continue
                bound = f"__rai_bound_{rule.id}"
                lookup = table.select(
                    pl.col(key).str.strip_chars().alias(f"__rai_key_{rule.id}"),
                    pl.col(column).str.strip_chars().cast(pl.Float64, strict=False).alias(bound),
                ).unique(subset=f"__rai_key_{rule.id}", keep="first")
                plan.frame = plan.frame.with_columns(
                    pl.col(key).str.strip_chars().alias(f"__rai_key_{rule.id}")
                ).join(lookup, on=f"__rai_key_{rule.id}", how="left", maintain_order="left")
                value = _typed(name, column_type)
                plan.failures[rule.id] = (value > pl.col(bound)).fill_null(value=False)
            case "freshness":
                stamp = _typed(name, column_type)
                reference = as_of
                if reference is None:
                    newest = plan.frame.select(stamp.max()).item()
                    reference = newest if isinstance(newest, datetime) else None
                    notes.append("freshness measured against the newest timestamp in the batch")
                if reference is None:
                    plan.problems[rule.id] = "no valid timestamps to measure freshness against"
                    continue
                cutoff = reference - timedelta(seconds=rule.params["max_lag_seconds"])
                if column_type.base == "date":
                    plan.failures[rule.id] = (stamp < cutoff.date()).fill_null(value=False)
                else:
                    plan.failures[rule.id] = (stamp < cutoff).fill_null(value=False)
    return plan, notes


def validate(
    contract: Contract,
    batch: pl.DataFrame,
    *,
    references: Mapping[str, pl.DataFrame] | None = None,
    as_of: datetime | None = None,
    history: KeyStore | None = None,
    batch_id: str | None = None,
    record: bool = False,
) -> ValidationReport:
    """Evaluate every rule of ``contract`` on ``batch`` (all columns as text).

    With ``record=True`` the batch's keys are added to ``history`` after evaluation,
    attributed to ``batch_id``; a batch is never compared with itself.
    """
    if record and (history is None or batch_id is None):
        raise ValueError("record=True needs both history and batch_id")
    started = time.perf_counter()
    rules = contract.rules()
    text_batch = batch.with_columns(pl.all().cast(pl.Utf8))
    inputs = _Inputs(references or {}, as_of, history, batch_id)
    plan, notes = _plan(contract, rules, text_batch, inputs)
    row_count = text_batch.height

    flags = (
        plan.frame.select([expr.alias(rule_id) for rule_id, expr in plan.failures.items()])
        if plan.failures
        else pl.DataFrame()
    )
    changes = tuple(detect_drift(contract, text_batch))
    results = []
    for rule in rules:

        def result(outcome: Outcome, rule: Rule = rule, **extra: Any) -> RuleResult:
            return RuleResult(
                rule_id=rule.id,
                kind=rule.kind,
                columns=rule.columns,
                severity=rule.severity,
                outcome=outcome,
                row_count=row_count,
                threshold=str(rule.threshold),
                **extra,
            )

        if rule.kind == "schema":
            breaking = [c for c in changes if c.impact == "breaking"]
            results.append(
                result(
                    "fail" if breaking else "pass",
                    failing_count=len(breaking),
                    message="; ".join(c.describe() for c in changes),
                )
            )
            continue
        if rule.id in plan.problems:
            results.append(result("error", message=plan.problems[rule.id]))
            continue
        rows = flags.get_column(rule.id).arg_true()
        failing = rows.len()
        passed = rule.threshold.allows(failing, row_count)
        results.append(
            result(
                "pass" if passed else "fail",
                failing_count=failing,
                sample_rows=tuple(rows.head(SAMPLE_LIMIT).to_list()),
                message=f"{failing} of {row_count} rows fail" if failing else "",
            )
        )
    if record and history is not None and batch_id is not None:
        for scope, keys in plan.keys_to_record.items():
            history.record(scope, keys, batch_id)
    return ValidationReport(
        contract_ref=contract.ref,
        contract_sha256=contract.spec_sha256,
        row_count=row_count,
        results=tuple(results),
        duration_seconds=time.perf_counter() - started,
        as_of=as_of,
        notes=tuple(dict.fromkeys(notes)),
        schema_changes=changes,
    )


def failing_rows(
    contract: Contract,
    batch: pl.DataFrame,
    rule_id: str,
    *,
    references: Mapping[str, pl.DataFrame] | None = None,
    as_of: datetime | None = None,
    history: KeyStore | None = None,
    batch_id: str | None = None,
) -> list[int]:
    """Every failing row of one rule (not just the sample), for scoring and debugging.

    Read-only: it never records keys in ``history``.
    """
    rules = [rule for rule in contract.rules() if rule.id == rule_id]
    if not rules:
        raise KeyError(rule_id)
    text_batch = batch.with_columns(pl.all().cast(pl.Utf8))
    inputs = _Inputs(references or {}, as_of, history, batch_id)
    plan, _ = _plan(contract, rules, text_batch, inputs)
    if rule_id not in plan.failures:
        return []
    return plan.frame.select(plan.failures[rule_id]).to_series().arg_true().to_list()


__all__ = [
    "RuleResult",
    "ValidationReport",
    "failing_rows",
    "read_batch",
    "validate",
]
