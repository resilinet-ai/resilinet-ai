"""Distribution drift between a reference and a current dataset (FR-09.2): PSI and KS.

A ``drift_check`` test case compares columns of its target asset (the *current*
data, e.g. this week's batch, or a model's scores) with a *reference* asset (e.g.
the batch a model was trained or approved on). Pure functions over polars frames.

* **PSI**, the population stability index, for numeric and categorical columns:
  ``sum((c - r) * ln(c / r))`` over bins, where ``r`` and ``c`` are the shares of
  reference and current values in each bin. Numeric bins are the reference's
  quantiles (``bins`` of them, deciles by default), so each holds about the same
  share of the reference; values outside the reference's range fall in the end
  bins, and a column with few distinct values gets fewer bins. Categorical bins are
  the categories of both sides. An empty bin counts as a share of ``EPSILON`` so the
  logarithm stays finite. Usual reading: below 0.1
  stable, 0.1 to 0.25 a moderate shift, above 0.25 a significant one.
* **KS**, the two-sample Kolmogorov-Smirnov statistic ``D`` for numeric columns: the
  largest gap between the two empirical distribution functions (0 identical, 1
  disjoint), with its asymptotic p-value. With 100,000 rows almost any difference
  is "significant", so the alert is on ``D`` itself, an effect size; the p-value
  is reported, not judged.

Null values are left out of both statistics and reported as a share. Every result
carries both sample sizes (FR-09.1's rule applies here too: no number without its
n), and a column with fewer than ``min_rows`` values on either side is an error,
not a pass: too little data to say anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

EPSILON = 1e-4
PSI_BANDS = ((0.1, "stable"), (0.25, "moderate shift"), (math.inf, "significant shift"))


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    asset: str = Field(min_length=1, max_length=200)  # an asset of the same project
    connector: str = Field(min_length=1, max_length=100)


class DriftColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["numeric", "categorical"] = "numeric"
    psi_alert: float = Field(default=0.25, gt=0, le=10)
    ks_alert: float | None = Field(default=0.1, gt=0, le=1)  # numeric only; None: PSI alone


class DriftDefinition(BaseModel):
    """What a ``drift_check`` test case's ``definition`` holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connector: str = Field(min_length=1, max_length=100)  # reads the target (current) asset
    environment: str
    reference: Reference
    columns: tuple[DriftColumn, ...] = Field(min_length=1, max_length=30)
    bins: int = Field(default=10, ge=2, le=50)
    min_rows: int = Field(default=100, ge=1, le=10_000_000)

    @field_validator("columns")
    @classmethod
    def _unique(cls, columns: tuple[DriftColumn, ...]) -> tuple[DriftColumn, ...]:
        names = [c.name for c in columns]
        if len(set(names)) != len(names):
            raise ValueError("a column is listed twice")
        return columns


@dataclass(frozen=True)
class ColumnDrift:
    column: str
    kind: str
    reference_rows: int  # values compared (nulls left out)
    current_rows: int
    reference_null_share: float
    current_null_share: float
    psi: float | None = None
    ks_statistic: float | None = None
    ks_p_value: float | None = None
    alerts: tuple[str, ...] = ()
    error: str | None = None
    bins: list[dict[str, Any]] = field(default_factory=list)  # the shares PSI used

    @property
    def band(self) -> str | None:
        if self.psi is None:
            return None
        return next(name for limit, name in PSI_BANDS if self.psi < limit)

    @property
    def outcome(self) -> str:
        return "error" if self.error else ("fail" if self.alerts else "pass")


def psi(reference: list[float], current: list[float]) -> float:
    """PSI over matching bin shares (each list sums to 1)."""
    total = 0.0
    for r, c in zip(reference, current, strict=True):
        r, c = max(r, EPSILON), max(c, EPSILON)
        total += (c - r) * math.log(c / r)
    return total


def ks_p_value(statistic: float, n: int, m: int) -> float:
    """Asymptotic two-sided p-value of the two-sample KS statistic (Kolmogorov's
    distribution with Stephens' small-sample correction)."""
    if statistic <= 0:
        return 1.0
    effective = math.sqrt(n * m / (n + m))
    lam = (effective + 0.12 + 0.11 / effective) * statistic
    total, sign = 0.0, 1.0
    for j in range(1, 101):
        term = math.exp(-2.0 * j * j * lam * lam)
        total += sign * term
        if term < 1e-12:
            break
        sign = -sign
    return min(1.0, max(0.0, 2.0 * total))


def ks_statistic(reference: pl.Series, current: pl.Series) -> float:
    """The largest gap between the two empirical CDFs, evaluated at every value."""
    n, m = reference.len(), current.len()
    both = pl.concat([
        pl.DataFrame({"v": reference.cast(pl.Float64), "r": 1, "c": 0}),
        pl.DataFrame({"v": current.cast(pl.Float64), "r": 0, "c": 1}),
    ]).sort("v")  # fmt: skip
    steps = (
        both.with_columns(pl.col("r").cum_sum().alias("cr"), pl.col("c").cum_sum().alias("cc"))
        .group_by("v", maintain_order=True)
        .agg(pl.col("cr").last(), pl.col("cc").last())  # ties: the CDFs after the last one
    )
    gaps = (steps["cr"].cast(pl.Float64) / n - steps["cc"].cast(pl.Float64) / m).abs()
    return float(gaps.max() or 0.0)  # type: ignore[arg-type]


def _numeric_bins(reference: pl.Series, current: pl.Series, bins: int) -> list[dict[str, Any]]:
    """Reference quantile edges; the end bins are open, so every value has a bin.

    Edges are observed values (``lower`` quantiles), so discrete data is not split
    between values, and never the reference's maximum, so no bin is empty by
    construction. Repeated quantiles merge: a column with few distinct values gets
    fewer, wider bins."""
    top = float(reference.max())  # type: ignore[arg-type]
    quantiles = [reference.quantile(i / bins, interpolation="lower") for i in range(1, bins)]
    edges = sorted({float(q) for q in quantiles if q is not None and float(q) < top})
    result = []
    lower = -math.inf
    for upper in [*edges, math.inf]:
        in_ref = ((reference > lower) & (reference <= upper)).sum()
        in_cur = ((current > lower) & (current <= upper)).sum()
        result.append({
            "from": None if lower == -math.inf else lower,
            "to": None if upper == math.inf else upper,
            "reference_share": float(in_ref) / reference.len(),
            "current_share": float(in_cur) / current.len(),
        })  # fmt: skip
        lower = upper
    return result


def _categorical_bins(reference: pl.Series, current: pl.Series) -> list[dict[str, Any]]:
    ref_counts = dict(reference.cast(pl.String).value_counts().iter_rows())
    cur_counts = dict(current.cast(pl.String).value_counts().iter_rows())
    return [
        {"category": category,
         "reference_share": ref_counts.get(category, 0) / reference.len(),
         "current_share": cur_counts.get(category, 0) / current.len()}
        for category in sorted(set(ref_counts) | set(cur_counts), key=str)
    ]  # fmt: skip


def column_drift(
    reference: pl.DataFrame, current: pl.DataFrame, column: DriftColumn, *, bins: int,
    min_rows: int,
) -> ColumnDrift:  # fmt: skip
    """PSI (and KS for a numeric column) of one column, with sample sizes and alerts."""
    name = column.name
    missing = [side for side, frame in (("reference", reference), ("current", current))
               if name not in frame.columns]  # fmt: skip
    if missing:
        where = " or ".join(missing)
        return ColumnDrift(name, column.kind, 0, 0, 0.0, 0.0,
                           error=f"column {name!r} is not in the {where} data")  # fmt: skip
    ref_all, cur_all = reference[name], current[name]
    if column.kind == "numeric":
        try:
            ref_all, cur_all = ref_all.cast(pl.Float64), cur_all.cast(pl.Float64)
        except pl.exceptions.InvalidOperationError:
            return ColumnDrift(name, column.kind, 0, 0, 0.0, 0.0,
                               error=f"column {name!r} is not numeric")  # fmt: skip
    ref, cur = ref_all.drop_nulls(), cur_all.drop_nulls()
    if column.kind == "numeric":
        ref, cur = ref.filter(ref.is_finite()), cur.filter(cur.is_finite())
    shares = (
        round(1 - ref.len() / ref_all.len(), 6) if ref_all.len() else 0.0,
        round(1 - cur.len() / cur_all.len(), 6) if cur_all.len() else 0.0,
    )
    base = ColumnDrift(name, column.kind, ref.len(), cur.len(), *shares)
    if ref.len() < min_rows or cur.len() < min_rows:
        few = f"{ref.len()} reference and {cur.len()} current, at least {min_rows} each"
        return replace(base, error=f"too few values to judge drift: {few}")
    table = (_numeric_bins(ref, cur, bins) if column.kind == "numeric"
             else _categorical_bins(ref, cur))  # fmt: skip
    value = round(
        psi([b["reference_share"] for b in table], [b["current_share"] for b in table]), 6
    )
    alerts = []
    if value >= column.psi_alert:
        alerts.append(f"PSI {value:.4f} >= {column.psi_alert}")
    statistic = p_value = None
    if column.kind == "numeric":
        statistic = round(ks_statistic(ref, cur), 6)
        p_value = ks_p_value(statistic, ref.len(), cur.len())
        if column.ks_alert is not None and statistic >= column.ks_alert:
            alerts.append(f"KS D {statistic:.4f} >= {column.ks_alert}")
    return replace(
        base,
        psi=value,
        ks_statistic=statistic,
        ks_p_value=p_value,
        alerts=tuple(alerts),
        bins=table,
    )


def drift(
    reference: pl.DataFrame, current: pl.DataFrame, definition: DriftDefinition
) -> list[ColumnDrift]:
    return [
        column_drift(reference, current, column, bins=definition.bins,
                     min_rows=definition.min_rows)
        for column in definition.columns
    ]  # fmt: skip
