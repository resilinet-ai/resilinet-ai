"""Model evaluation with sample sizes (FR-09.1): a binary classifier's scores against
their labels.

A ``tevv_metric`` test case reads its target asset, a table of one score and one
label per row (SandboxBank's ``sandboxbank model score`` writes ``txn_id, score,
is_fraud``), and judges the metrics it lists against stated targets. Pure functions
over polars frames.

* **No number without its n.** Every metric carries its numerator and denominator
  (recall is 99 of 202 frauds caught, not 49%), the confusion matrix is recorded
  beside it, and every proportion has a confidence interval: Wilson's score
  interval, which stays inside [0, 1] and behaves at small counts. ROC AUC's
  interval uses Hanley and McNeil's (1982) standard error, an approximation; F1
  has none.
* **Too little data is an error, not a pass.** Fewer than ``min_rows`` usable rows,
  or fewer than ``min_positives`` positives or ``min_negatives`` negatives, and
  nothing is judged: 3 of 4 frauds caught is not evidence of 75% recall.
* **Judging.** ``judge_on: estimate`` (the default) compares the metric itself with
  its target. ``confidence_bound`` is stricter: an ``at_least`` target must be met
  by the interval's lower end and an ``at_most`` by its upper end, so a pass means
  the data supports the target at the stated confidence.
* **Rows.** A row flags positive when ``score >= threshold``. A null score or label
  leaves the row out and is reported as a count. A score that is not a finite
  number, or a label outside true/false/1/0, is an error: the data is not what
  the test says it is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from statistics import NormalDist
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MetricName = Literal[
    "accuracy", "precision", "recall", "specificity", "false_positive_rate", "f1", "roc_auc"
]
POSITIVE = frozenset({"true", "1", "1.0"})
NEGATIVE = frozenset({"false", "0", "0.0"})
# What each proportion counts: (numerator, denominator) over the confusion matrix.
_DESCRIBED = {
    "accuracy": "correct of all rows",
    "precision": "true positives of rows flagged",
    "recall": "true positives of all positives",
    "specificity": "true negatives of all negatives",
    "false_positive_rate": "false positives of all negatives",
    "f1": "harmonic mean of precision and recall",
    "roc_auc": "chance a random positive scores above a random negative",
}


class MetricTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: MetricName
    at_least: float | None = Field(default=None, ge=0, le=1)
    at_most: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _bounded(self) -> MetricTarget:
        if self.at_least is None and self.at_most is None:
            raise ValueError(f"{self.name} needs at_least or at_most")
        if (
            self.at_least is not None
            and self.at_most is not None
            and (self.at_least > self.at_most)
        ):
            raise ValueError(f"{self.name}: at_least is above at_most")
        return self


class MetricDefinition(BaseModel):
    """What a ``tevv_metric`` test case's ``definition`` holds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    connector: str = Field(min_length=1, max_length=100)  # reads the target (scores) asset
    environment: str
    score_column: str = Field(default="score", min_length=1, max_length=200)
    label_column: str = Field(default="is_fraud", min_length=1, max_length=200)
    threshold: float = Field(default=0.5, allow_inf_nan=False)
    metrics: tuple[MetricTarget, ...] = Field(min_length=1, max_length=7)
    judge_on: Literal["estimate", "confidence_bound"] = "estimate"
    confidence: float = Field(default=0.95, ge=0.5, le=0.999)
    min_rows: int = Field(default=100, ge=1, le=10_000_000)
    min_positives: int = Field(default=30, ge=1, le=10_000_000)
    min_negatives: int = Field(default=30, ge=1, le=10_000_000)

    @field_validator("metrics")
    @classmethod
    def _unique(cls, metrics: tuple[MetricTarget, ...]) -> tuple[MetricTarget, ...]:
        names = [m.name for m in metrics]
        if len(set(names)) != len(names):
            raise ValueError("a metric is listed twice")
        return metrics

    @model_validator(mode="after")
    def _boundable(self) -> MetricDefinition:
        if self.judge_on == "confidence_bound" and any(m.name == "f1" for m in self.metrics):
            raise ValueError("f1 has no confidence interval; judge it with judge_on: estimate")
        return self


@dataclass(frozen=True)
class Confusion:
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def n(self) -> int:
        return (
            self.true_positives
            + self.false_positives
            + self.false_negatives
            + (self.true_negatives)
        )

    @property
    def positives(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def negatives(self) -> int:
        return self.false_positives + self.true_negatives

    @property
    def flagged(self) -> int:
        return self.true_positives + self.false_positives

    def counts(self, name: str) -> tuple[int, int] | None:
        """(numerator, denominator) of a proportion; None for F1 and ROC AUC."""
        tp, fp, fn, tn = (self.true_positives, self.false_positives, self.false_negatives,
                          self.true_negatives)  # fmt: skip
        return {
            "accuracy": (tp + tn, self.n),
            "precision": (tp, tp + fp),
            "recall": (tp, tp + fn),
            "specificity": (tn, tn + fp),
            "false_positive_rate": (fp, fp + tn),
        }.get(name)

    def as_dict(self) -> dict[str, int]:
        return {
            "true_positives": self.true_positives, "false_positives": self.false_positives,
            "false_negatives": self.false_negatives, "true_negatives": self.true_negatives,
        }  # fmt: skip


@dataclass(frozen=True)
class MetricResult:
    target: MetricTarget
    value: float | None
    numerator: int | None  # for a proportion
    denominator: int | None
    low: float | None  # the confidence interval
    high: float | None
    problem: str | None = None  # a missed target
    error: str | None = None  # could not be judged

    @property
    def name(self) -> str:
        return self.target.name

    @property
    def outcome(self) -> str:
        return "error" if self.error else ("fail" if self.problem else "pass")


@dataclass(frozen=True)
class Evaluation:
    rows_read: int
    null_rows: int  # left out: a null score or label
    confusion: Confusion | None
    results: list[MetricResult] = field(default_factory=list)
    error: str | None = None  # the sample as a whole cannot be judged

    @property
    def problems(self) -> list[str]:
        if self.error:
            return [self.error]
        return [f"{r.name}: {r.error or r.problem}" for r in self.results
                if r.error or r.problem]  # fmt: skip


def z_value(confidence: float) -> float:
    return NormalDist().inv_cdf((1 + confidence) / 2)


def wilson(successes: int, n: int, confidence: float) -> tuple[float, float]:
    """Wilson's score interval for a proportion ``successes / n``."""
    if n <= 0:
        raise ValueError("no trials")
    z = z_value(confidence)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return low, high


def roc_auc(labels: pl.Series, scores: pl.Series) -> float:
    """The Mann-Whitney statistic: ties in score count as half."""
    positives = int(labels.sum())
    negatives = labels.len() - positives
    if not positives or not negatives:
        raise ValueError("ROC AUC needs both positives and negatives")
    ranks = scores.rank(method="average")
    rank_sum = float(ranks.filter(labels).sum())
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def auc_interval(
    auc: float, positives: int, negatives: int, confidence: float
) -> tuple[float, float]:
    """Hanley and McNeil's (1982) normal approximation, clipped to [0, 1]."""
    q1 = auc / (2 - auc)
    q2 = 2 * auc * auc / (1 + auc)
    variance = (auc * (1 - auc) + (positives - 1) * (q1 - auc * auc)
                + (negatives - 1) * (q2 - auc * auc)) / (positives * negatives)  # fmt: skip
    half = z_value(confidence) * math.sqrt(max(variance, 0.0))
    return max(0.0, auc - half), min(1.0, auc + half)


def _parse(
    frame: pl.DataFrame, definition: MetricDefinition
) -> tuple[pl.DataFrame, int, str | None]:
    """(score, label) rows with nulls left out, the number left out, or an error."""
    missing = [c for c in (definition.score_column, definition.label_column)
               if c not in frame.columns]  # fmt: skip
    if missing:
        return pl.DataFrame(), 0, f"column(s) {', '.join(map(repr, missing))} not in the data"
    label = pl.col(definition.label_column).cast(pl.String).str.strip_chars().str.to_lowercase()
    raw = frame.select(
        pl.col(definition.score_column).cast(pl.String).str.strip_chars().alias("score"),
        label.alias("label"),
    ).with_columns(
        pl.when(pl.col("score") == "").then(None).otherwise(pl.col("score")).alias("score"),
        pl.when(pl.col("label") == "").then(None).otherwise(pl.col("label")).alias("label"),
    )
    present = raw.drop_nulls()
    null_rows = raw.height - present.height
    parsed = present.with_columns(
        pl.col("score").cast(pl.Float64, strict=False).alias("value"),
        pl.col("label").is_in(list(POSITIVE)).alias("positive"),
    )
    bad_scores = parsed.filter(pl.col("value").is_null() | ~pl.col("value").is_finite())
    if bad_scores.height:
        example = bad_scores["score"][0]
        return (
            pl.DataFrame(),
            null_rows,
            (f"{bad_scores.height} score(s) are not finite numbers, e.g. {example!r}"),
        )
    bad_labels = parsed.filter(~pl.col("label").is_in(list(POSITIVE | NEGATIVE)))
    if bad_labels.height:
        example = bad_labels["label"][0]
        return (
            pl.DataFrame(),
            null_rows,
            (f"{bad_labels.height} label(s) are not true/false or 1/0, e.g. {example!r}"),
        )
    return parsed.select("value", "positive"), null_rows, None


def _value(name: str, confusion: Confusion, auc: float | None) -> float | None:
    if name == "roc_auc":
        return auc
    if name == "f1":
        tp = confusion.true_positives
        denominator = 2 * tp + confusion.false_positives + confusion.false_negatives
        return 2 * tp / denominator if denominator else None
    counts = confusion.counts(name)
    if counts is None or not counts[1]:
        return None
    return counts[0] / counts[1]


def _judge(result: MetricResult, definition: MetricDefinition) -> MetricResult:
    target = result.target
    if result.value is None:
        why = "nothing was flagged" if target.name == "precision" else "its denominator is 0"
        return replace(result, error=f"undefined: {why}")
    bounded = definition.judge_on == "confidence_bound"
    problems = []
    if target.at_least is not None:
        judged = result.low if bounded else result.value
        assert judged is not None  # noqa: S101 - f1 is refused with confidence_bound
        if judged < target.at_least:
            what = "lower bound" if bounded else "value"
            problems.append(f"{what} {judged:.4f} is below {target.at_least}")
    if target.at_most is not None:
        judged = result.high if bounded else result.value
        assert judged is not None  # noqa: S101 - f1 is refused with confidence_bound
        if judged > target.at_most:
            what = "upper bound" if bounded else "value"
            problems.append(f"{what} {judged:.4f} is above {target.at_most}")
    if not problems:
        return result
    return replace(result, problem=f"{'; '.join(problems)} ({_summary(result)})")


def _summary(result: MetricResult) -> str:
    text = f"{result.value:.4f}" if result.value is not None else "undefined"
    if result.denominator is not None:
        text += f" = {result.numerator:,} of {result.denominator:,}"
    if result.low is not None and result.high is not None:
        text += f", interval {result.low:.4f} to {result.high:.4f}"
    return text


def evaluate(frame: pl.DataFrame, definition: MetricDefinition) -> Evaluation:
    """Every listed metric with its counts and interval, judged against its target."""
    rows, null_rows, error = _parse(frame, definition)
    if error:
        return Evaluation(frame.height, null_rows, None, error=error)
    flagged = rows["value"] >= definition.threshold
    positive = rows["positive"]
    confusion = Confusion(
        true_positives=int((flagged & positive).sum()),
        false_positives=int((flagged & ~positive).sum()),
        false_negatives=int((~flagged & positive).sum()),
        true_negatives=int((~flagged & ~positive).sum()),
    )
    short = []
    if confusion.n < definition.min_rows:
        short.append(f"{confusion.n:,} usable rows, at least {definition.min_rows:,}")
    if confusion.positives < definition.min_positives:
        short.append(f"{confusion.positives:,} positives, at least {definition.min_positives:,}")
    if confusion.negatives < definition.min_negatives:
        short.append(f"{confusion.negatives:,} negatives, at least {definition.min_negatives:,}")
    auc = None
    if confusion.positives and confusion.negatives:
        auc = roc_auc(positive, rows["value"])
    results = []
    for target in definition.metrics:
        value = _value(target.name, confusion, auc)
        counts = confusion.counts(target.name)
        low = high = None
        if value is not None and counts is not None:
            low, high = wilson(*counts, definition.confidence)
        elif value is not None and target.name == "roc_auc":
            low, high = auc_interval(value, confusion.positives, confusion.negatives,
                                     definition.confidence)  # fmt: skip
        result = MetricResult(
            target, None if value is None else round(value, 6),
            counts[0] if counts else None, counts[1] if counts else None,
            None if low is None else round(low, 6), None if high is None else round(high, 6),
        )  # fmt: skip
        if short:  # reported, never judged
            results.append(replace(result, error="too few rows to judge"))
        else:
            results.append(_judge(result, definition))
    return Evaluation(
        frame.height, null_rows, confusion, results,
        error=f"too few rows to judge the model: {'; '.join(short)}" if short else None,
    )  # fmt: skip


def describe(name: str) -> str:
    return _DESCRIBED[name]
