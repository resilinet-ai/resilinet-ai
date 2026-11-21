"""Robustness tests (FR-09.3): perturbed and missing-feature inputs, scored and reported.

A ``robustness_check`` test case reads a feature table (one row per case: an id, the
model's input features and the label; SandboxBank's ``sandboxbank model features``
writes one). RAI perturbs the table **itself** and has every variant scored by the
model of the system under test, through a scoring target on the environment's
allow-list. RAI never runs the model (decision TEVV-METRIC), but the evidence shows
exactly what was perturbed.

* **Perturbations are RAI's, seeded and recorded.** ``noise`` multiplies a numeric
  feature by ``1 + scale * z`` (z standard normal); ``missing`` sets a feature to
  missing; ``unknown_category`` puts a value outside a categorical feature's
  vocabulary. Each picks a ``share`` of the rows. Every draw is a SHA-256 of the seed,
  the perturbation, the column and the row's position, so a variant depends on nothing
  but the definition and the table (whose digest the run's lineage records): anyone
  can rebuild it and check the SHA-256 of the rows sent (``inputs_digest``).
* **Every metric with its n.** The clean table and each variant are judged with the
  model-evaluation code of FR-09.1: counts, Wilson or Hanley-McNeil intervals, and the
  same minimum sample sizes. Too little data is an error, never a pass.
* **Judged on degradation.** For each metric, the degradation is how much worse the
  variant is than the clean table (lower, or higher for ``false_positive_rate``). More
  than ``max_drop`` fails the variant. A model whose identity changes between calls is
  an error: the variants would not be comparable.

Pure functions over polars frames; the scoring call is passed in.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rai.tevv.metrics import (
    NEGATIVE,
    POSITIVE,
    Evaluation,
    MetricDefinition,
    MetricName,
    MetricResult,
    MetricTarget,
    evaluate,
)

NAME = r"^[a-z][a-z0-9_.-]{0,62}$"
LOWER_IS_BETTER = frozenset({"false_positive_rate"})
# A scorer takes rows of named features and returns one score per row, and the identity
# of the model that scored them.
Scorer = Callable[[list[dict[str, float | str | None]]], tuple[list[float], dict[str, Any]]]


class Perturbation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=NAME)
    kind: Literal["noise", "missing", "unknown_category"]
    columns: tuple[str, ...] = Field(min_length=1, max_length=50)
    share: float = Field(default=1.0, gt=0, le=1)
    scale: float | None = Field(default=None, gt=0, le=10)  # noise only
    value: str = Field(default="__unknown__", min_length=1, max_length=100)

    @model_validator(mode="after")
    def _fits_kind(self) -> Perturbation:
        if self.kind == "noise" and self.scale is None:
            raise ValueError(f"{self.name}: noise needs a scale, e.g. 0.1 for 10%")
        if self.kind != "noise" and self.scale is not None:
            raise ValueError(f"{self.name}: only noise has a scale")
        return self


class MetricDrop(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: MetricName
    max_drop: float = Field(ge=0, le=1)


class RobustnessDefinition(BaseModel):
    """What a ``robustness_check`` test case's ``definition`` holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connector: str = Field(min_length=1, max_length=100)  # reads the feature table
    environment: str
    scorer: str = Field(pattern=NAME)  # an http target with a score_url
    features: tuple[str, ...] = Field(min_length=1, max_length=200)
    categorical: tuple[str, ...] = ()  # sent as text; every other feature as a number
    id_column: str = Field(default="txn_id", min_length=1, max_length=200)
    label_column: str = Field(default="is_fraud", min_length=1, max_length=200)
    threshold: float = Field(default=0.5, allow_inf_nan=False)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    perturbations: tuple[Perturbation, ...] = Field(min_length=1, max_length=20)
    metrics: tuple[MetricDrop, ...] = Field(min_length=1, max_length=7)
    confidence: float = Field(default=0.95, ge=0.5, le=0.999)
    min_rows: int = Field(default=100, ge=1, le=10_000_000)
    min_positives: int = Field(default=30, ge=1, le=10_000_000)
    min_negatives: int = Field(default=30, ge=1, le=10_000_000)
    max_rows: int = Field(default=100_000, ge=1, le=1_000_000)  # bounds the scoring calls
    batch_size: int = Field(default=1_000, ge=1, le=5_000)

    @field_validator("features", "categorical")
    @classmethod
    def _distinct(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(names)) != len(names):
            raise ValueError("a column is listed twice")
        return names

    @model_validator(mode="after")
    def _consistent(self) -> RobustnessDefinition:
        features = set(self.features)
        if not set(self.categorical) <= features:
            raise ValueError("every categorical column must be one of the features")
        if {self.id_column, self.label_column} & features:
            raise ValueError("the id and label columns are not features")
        names = [p.name for p in self.perturbations]
        if len(set(names)) != len(names) or "clean" in names:
            raise ValueError("perturbation names must be distinct and not 'clean'")
        for p in self.perturbations:
            unknown = sorted(set(p.columns) - features)
            if unknown:
                raise ValueError(f"{p.name}: {unknown} are not features")
            if p.kind == "noise" and set(p.columns) & set(self.categorical):
                raise ValueError(f"{p.name}: noise applies to numeric features only")
            if p.kind == "unknown_category" and not set(p.columns) <= set(self.categorical):
                raise ValueError(f"{p.name}: unknown_category applies to categorical features")
        if len({m.name for m in self.metrics}) != len(self.metrics):
            raise ValueError("a metric is listed twice")
        return self

    def evaluation(self) -> MetricDefinition:
        """FR-09.1's evaluation of one variant: counts and intervals, no targets of its
        own (``at_least: 0`` always holds); robustness judges the degradation."""
        return MetricDefinition(
            connector=self.connector, environment=self.environment, score_column="score",
            label_column="label", threshold=self.threshold,
            metrics=tuple(MetricTarget(name=m.name, at_least=0.0) for m in self.metrics),
            confidence=self.confidence, min_rows=self.min_rows,
            min_positives=self.min_positives, min_negatives=self.min_negatives,
        )  # fmt: skip


@dataclass(frozen=True)
class Degradation:
    name: str
    clean: float | None
    value: float | None
    low: float | None
    high: float | None
    numerator: int | None
    denominator: int | None
    degradation: float | None
    max_drop: float
    problem: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean, "value": self.value, "low": self.low, "high": self.high,
            "numerator": self.numerator, "denominator": self.denominator,
            "degradation": self.degradation, "max_drop": self.max_drop,
            "problem": self.problem, "error": self.error,
        }  # fmt: skip


@dataclass(frozen=True)
class Variant:
    """The clean table or one perturbation of it, scored and judged."""

    name: str
    perturbation: Perturbation | None
    rows: int
    rows_perturbed: int
    cells_changed: int
    digest: str  # SHA-256 of the rows sent, as canonical JSON (``sent_digest``)
    evaluation: Evaluation | None
    degradations: list[Degradation] = field(default_factory=list)
    error: str | None = None

    @property
    def outcome(self) -> str:
        if self.error or (self.evaluation is not None and self.evaluation.error):
            return "error"
        if any(d.error for d in self.degradations):
            return "error"
        return "fail" if any(d.problem for d in self.degradations) else "pass"

    @property
    def problems(self) -> list[str]:
        if self.error:
            return [f"{self.name}: {self.error}"]
        if self.evaluation is not None and self.evaluation.error:
            return [f"{self.name}: {self.evaluation.error}"]
        return [f"{self.name}: {d.name} {d.error or d.problem}" for d in self.degradations
                if d.error or d.problem]  # fmt: skip


@dataclass(frozen=True)
class Robustness:
    rows_read: int
    rows_used: int
    null_label_rows: int
    model: dict[str, Any] | None
    variants: list[Variant] = field(default_factory=list)  # the clean table first
    error: str | None = None

    @property
    def problems(self) -> list[str]:
        if self.error:
            return [self.error]
        return [problem for variant in self.variants for problem in variant.problems]


def uniform(seed: int, *parts: object) -> float:
    """A draw in [0, 1) from SHA-256: the same for the same seed and parts, anywhere."""
    key = ":".join([str(seed), *(str(p) for p in parts)]).encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") / 2**64


def normal(seed: int, *parts: object) -> float:
    """A standard normal draw (Box-Muller over two ``uniform`` draws)."""
    u1 = max(uniform(seed, *parts, "u1"), 1e-300)
    u2 = uniform(seed, *parts, "u2")
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


def inputs(frame: pl.DataFrame, definition: RobustnessDefinition) -> tuple[pl.DataFrame, int]:
    """The feature table as typed inputs (numbers and text, missing as null) with the
    label as ``label``; rows without a label are left out and counted. Raises
    ValueError when the data is not what the test says it is."""
    needed = [definition.id_column, *definition.features, definition.label_column]
    missing = [c for c in needed if c not in frame.columns]
    if missing:
        raise ValueError(f"column(s) {', '.join(map(repr, missing))} not in the data")
    as_text = {c: pl.col(c).cast(pl.String).str.strip_chars() for c in needed}
    cleaned = frame.select(
        pl.when(expr == "").then(None).otherwise(expr).alias(c) for c, expr in as_text.items()
    )
    labels = cleaned[definition.label_column].str.to_lowercase()
    bad = labels.drop_nulls().filter(~labels.drop_nulls().is_in(list(POSITIVE | NEGATIVE)))
    if bad.len():
        raise ValueError(f"{bad.len()} label(s) are not true/false or 1/0, e.g. {bad[0]!r}")
    labelled = cleaned.with_columns(labels.alias("label")).filter(pl.col("label").is_not_null())
    columns = []
    for name in definition.features:
        if name in definition.categorical:
            columns.append(pl.col(name))
            continue
        number = pl.col(name).cast(pl.Float64, strict=False)
        columns.append(number.alias(name))
        unparsed = labelled.filter(pl.col(name).is_not_null() & number.is_null())
        if unparsed.height:
            raise ValueError(f"{name} is numeric, but {unparsed.height} value(s) are not "
                             f"numbers, e.g. {unparsed[name][0]!r}")  # fmt: skip
        if labelled.filter(number.is_not_null() & ~number.is_finite()).height:
            raise ValueError(f"{name} has values that are not finite numbers")
    typed = labelled.select(pl.col(definition.id_column), *columns, pl.col("label"))
    return typed, cleaned.height - labelled.height


def perturb(
    frame: pl.DataFrame, perturbation: Perturbation, seed: int
) -> tuple[pl.DataFrame, int, int]:
    """The variant, the number of rows picked and the number of cells changed."""
    picked = [uniform(seed, perturbation.name, "pick", i) < perturbation.share
              for i in range(frame.height)]  # fmt: skip
    rows = sum(picked)
    changed = 0
    replaced = []
    for column in perturbation.columns:
        before = frame[column].to_list()
        after: list[Any] = []
        for i, (value, pick) in enumerate(zip(before, picked, strict=True)):
            if not pick:
                after.append(value)
            elif perturbation.kind == "missing":
                after.append(None)
            elif perturbation.kind == "unknown_category":
                after.append(perturbation.value)
            elif value is None:  # noise on a missing number leaves it missing
                after.append(None)
            else:
                scale = perturbation.scale or 0.0
                after.append(value * (1 + scale * normal(seed, perturbation.name, column, i)))
            changed += after[-1] != value
        replaced.append(pl.Series(column, after, dtype=frame[column].dtype))
    return frame.with_columns(replaced), rows, changed


def _records(frame: pl.DataFrame, definition: RobustnessDefinition) -> list[dict[str, Any]]:
    return frame.select(definition.features).to_dicts()


def sent_digest(frame: pl.DataFrame, definition: RobustnessDefinition) -> str:
    """SHA-256 of every row sent to the scorer, in order, as canonical JSON: what
    anyone who rebuilds the variant can check."""
    digest = hashlib.sha256()
    for record in _records(frame, definition):
        digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _score(
    frame: pl.DataFrame, definition: RobustnessDefinition, scorer: Scorer
) -> tuple[list[float], dict[str, Any]]:
    """Every row's score, in batches; the model must be the same for every batch."""
    records = _records(frame, definition)
    scores: list[float] = []
    model: dict[str, Any] | None = None
    for start in range(0, len(records), definition.batch_size):
        batch = records[start : start + definition.batch_size]
        got, identity = scorer(batch)
        if len(got) != len(batch):
            raise ValueError(f"the scorer returned {len(got)} scores for {len(batch)} rows")
        if not all(isinstance(s, int | float) and math.isfinite(s) for s in got):
            raise ValueError("the scorer returned a score that is not a finite number")
        if model is not None and identity != model:
            raise ValueError(f"the model changed while scoring: {model} then {identity}")
        model = identity
        scores.extend(float(s) for s in got)
    return scores, model or {}


def _evaluated(frame: pl.DataFrame, scores: list[float], metric: MetricDefinition) -> Evaluation:
    judged = pl.DataFrame({"score": scores, "label": frame["label"].to_list()})
    return evaluate(judged, metric)


def _degradations(
    clean: Evaluation, variant: Evaluation, definition: RobustnessDefinition
) -> list[Degradation]:
    before = {r.name: r for r in clean.results}
    after = {r.name: r for r in variant.results}
    found = []
    for drop in definition.metrics:
        a: MetricResult | None = before.get(drop.name)
        b: MetricResult | None = after.get(drop.name)
        base = Degradation(
            drop.name, a.value if a else None, b.value if b else None, b.low if b else None,
            b.high if b else None, b.numerator if b else None, b.denominator if b else None,
            None, drop.max_drop,
        )  # fmt: skip
        if a is None or b is None or a.error or b.error or a.value is None or b.value is None:
            why = (b.error if b else None) or (a.error if a else None) or "undefined"
            found.append(_with(base, error=f"cannot be compared: {why}"))
            continue
        worse = b.value - a.value if drop.name in LOWER_IS_BETTER else a.value - b.value
        worse = round(worse, 6)
        problem = None
        if worse > drop.max_drop:
            problem = (f"degraded by {worse:.4f}, more than {drop.max_drop} "
                       f"({a.value:.4f} clean, {b.value:.4f} perturbed)")  # fmt: skip
        found.append(_with(base, degradation=worse, problem=problem))
    return found


def _with(base: Degradation, **changes: Any) -> Degradation:
    return Degradation(**{**base.__dict__, **changes})


def robustness(frame: pl.DataFrame, definition: RobustnessDefinition, scorer: Scorer) -> Robustness:
    """Score the clean table and every perturbation of it; judge each variant's
    degradation against ``max_drop``."""
    try:
        typed, null_labels = inputs(frame, definition)
    except ValueError as error:
        return Robustness(frame.height, 0, 0, None, error=str(error))
    if typed.height > definition.max_rows:
        return Robustness(frame.height, typed.height, null_labels, None,
                          error=f"{typed.height:,} rows, at most {definition.max_rows:,} "
                                "are scored (max_rows)")  # fmt: skip
    metric = definition.evaluation()
    try:
        clean_scores, model = _score(typed, definition, scorer)
    except ValueError as error:
        return Robustness(frame.height, typed.height, null_labels, None,
                          error=f"scoring the clean table: {error}")  # fmt: skip
    clean = _evaluated(typed, clean_scores, metric)
    variants = [
        Variant("clean", None, typed.height, 0, 0,
                sent_digest(typed, definition), clean)
    ]  # fmt: skip
    for perturbation in definition.perturbations:
        changed, rows, cells = perturb(typed, perturbation, definition.seed)
        sent = sent_digest(changed, definition)
        try:
            scores, identity = _score(changed, definition, scorer)
        except ValueError as error:
            variants.append(Variant(perturbation.name, perturbation, typed.height, rows, cells,
                                    sent, None, error=f"scoring: {error}"))  # fmt: skip
            continue
        if identity != model:
            variants.append(Variant(perturbation.name, perturbation, typed.height, rows, cells,
                                    sent, None, error=f"the model changed: {model} then "
                                    f"{identity}; the variants are not comparable"))  # fmt: skip
            continue
        evaluation = _evaluated(changed, scores, metric)
        judged = _degradations(clean, evaluation, definition)
        variants.append(Variant(perturbation.name, perturbation, typed.height, rows, cells, sent,
                                evaluation, judged))  # fmt: skip
    return Robustness(frame.height, typed.height, null_labels, model, variants)


def describe_variant(variant: Variant) -> dict[str, Any]:
    """What a variant's observation records (FR-09.3: what was perturbed, and n)."""
    evaluation = variant.evaluation
    confusion = evaluation.confusion if evaluation else None
    perturbation = variant.perturbation
    metrics: Sequence[MetricResult] = evaluation.results if evaluation else []
    return {
        "rows": variant.rows, "rows_perturbed": variant.rows_perturbed,
        "cells_changed": variant.cells_changed, "inputs_digest": variant.digest,
        "perturbation": perturbation.model_dump() if perturbation else None,
        "positives": confusion.positives if confusion else None,
        "negatives": confusion.negatives if confusion else None,
        "confusion": confusion.as_dict() if confusion else None,
        "metrics": {r.name: {"value": r.value, "numerator": r.numerator,
                             "denominator": r.denominator, "low": r.low, "high": r.high}
                    for r in metrics},
        "degradations": {d.name: d.as_dict() for d in variant.degradations},
        "error": variant.error or (evaluation.error if evaluation else None),
    }  # fmt: skip
