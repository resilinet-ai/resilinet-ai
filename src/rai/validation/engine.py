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

Rules owned by later backlog items (``schema``: B-11) report ``skipped`` with
that reason, so nothing passes silently.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import polars as pl

from rai.validation.contract import ColumnType, Contract, Rule, Severity
from rai.validation.history import KeyStore

Outcome = Literal["pass", "fail", "error", "skipped"]
SAMPLE_LIMIT = 20
ROW = "__rai_row"

DEFERRED: dict[str, str] = {
    "schema": "schema drift detection arrives with B-11",
}
KEY_SEPARATOR = "\x1f"  # joins composite key parts; never appears in delivered text

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_INTEGER = r"^-?[0-9]{1,18}$"
_TIMESTAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"


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

    def by_rule(self) -> dict[str, RuleResult]:
        return {result.rule_id: result for result in self.results}

    def with_outcome(self, outcome: Outcome) -> list[RuleResult]:
        return [result for result in self.results if result.outcome == outcome]


def read_batch(path: Path) -> pl.DataFrame:
    """Read a CSV batch with every column as text, as delivered."""
    return pl.read_csv(path, infer_schema=False)


# ---- expressions ---------------------------------------------------------------------


def _missing(name: str) -> pl.Expr:
    column = pl.col(name)
    return column.is_null() | (column.str.strip_chars() == "")


def _decimal_pattern(column_type: ColumnType) -> str:
    precision, scale = column_type.precision or 38, column_type.scale or 0
    whole = max(precision - scale, 1)
    fraction = rf"(\.[0-9]{{1,{scale}}})?" if scale else ""
    return rf"^-?[0-9]{{1,{whole}}}{fraction}$"


def _valid(name: str, column_type: ColumnType) -> pl.Expr:
    """True where a present value fits the declared type."""
    text = pl.col(name).str.strip_chars()
    match column_type.base:
        case "string":
            return pl.lit(value=True)
        case "uuid":
            return text.str.contains(_UUID)
        case "integer":
            return text.str.contains(_INTEGER)
        case "decimal":
            return text.str.contains(_decimal_pattern(column_type))
        case "float":
            return text.cast(pl.Float64, strict=False).is_not_null()
        case "boolean":
            return text.str.to_lowercase().is_in(["true", "false"])
        case "date":
            return text.str.to_date("%Y-%m-%d", strict=False).is_not_null()
        case "timestamp":
            return text.str.contains(_TIMESTAMP) & _as_timestamp(name).is_not_null()
    raise AssertionError(column_type)  # pragma: no cover - ColumnType is a closed set


def _as_timestamp(name: str) -> pl.Expr:
    iso = pl.col(name).str.strip_chars().str.replace(r"Z$", "+00:00")
    return iso.str.to_datetime(
        format="%Y-%m-%dT%H:%M:%S%.f%:z", strict=False, time_unit="us", time_zone="UTC"
    )


def _typed(name: str, column_type: ColumnType) -> pl.Expr:
    """The value in its declared type, or null where missing or malformed."""
    present = ~_missing(name) & _valid(name, column_type)
    text = pl.col(name).str.strip_chars()
    match column_type.base:
        case "integer" | "decimal" | "float":
            value = text.cast(pl.Float64, strict=False)
        case "date":
            value = text.str.to_date("%Y-%m-%d", strict=False)
        case "timestamp":
            value = _as_timestamp(name)
        case _:
            value = text
    return pl.when(present).then(value)


def _bound(value: Any, column_type: ColumnType) -> Any:
    if column_type.base in {"integer", "decimal", "float"}:
        return float(Decimal(str(value)))
    if column_type.base == "date":
        return datetime.fromisoformat(str(value)).date()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


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
        if rule.kind in DEFERRED:
            continue
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

        if rule.kind in DEFERRED:
            results.append(result("skipped", message=DEFERRED[rule.kind]))
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
    "DEFERRED",
    "RuleResult",
    "ValidationReport",
    "failing_rows",
    "read_batch",
    "validate",
]
