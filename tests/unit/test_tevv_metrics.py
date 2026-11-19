"""Model evaluation with sample sizes (FR-09.1): counts, intervals, the sample-size
rule and judging on the estimate or on the confidence bound."""

from __future__ import annotations

import random
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from rai.tevv.metrics import (
    MetricDefinition,
    auc_interval,
    evaluate,
    roc_auc,
    wilson,
)

BASE: dict[str, Any] = {"connector": "feed", "environment": "SANDBOX-01",
                        "min_rows": 1, "min_positives": 1, "min_negatives": 1}  # fmt: skip


def definition(*metrics: dict[str, Any], **extra: Any) -> MetricDefinition:
    return MetricDefinition.model_validate(BASE | {"metrics": list(metrics)} | extra)


def frame(scores: list[Any], labels: list[Any]) -> pl.DataFrame:
    return pl.DataFrame({"score": [None if s is None else str(s) for s in scores],
                         "is_fraud": [None if v is None else str(v) for v in labels]})  # fmt: skip


def synthetic(n: int, positives: int, seed: int = 7) -> pl.DataFrame:
    """Positives score higher on average; strings, as a connector returns them."""
    rng = random.Random(seed)  # noqa: S311 - test data, not secrets
    labels = [i < positives for i in range(n)]
    scores = [min(1.0, max(0.0, rng.gauss(0.7 if y else 0.3, 0.2))) for y in labels]
    return frame([f"{s:.6f}" for s in scores], ["true" if y else "false" for y in labels])


def test_confusion_and_counts_by_hand() -> None:
    data = frame([0.9, 0.2, 0.8, 0.1, 0.0, 0.5], ["true", "true", "false", "false", "0", "1"])
    result = evaluate(data, definition({"name": "recall", "at_least": 0.5},
                                       {"name": "precision", "at_least": 0.5},
                                       {"name": "false_positive_rate", "at_most": 0.5},
                                       {"name": "accuracy", "at_least": 0.5},
                                       {"name": "f1", "at_least": 0.5}))  # fmt: skip
    assert result.error is None
    assert result.confusion is not None
    assert result.confusion.as_dict() == {
        "true_positives": 2, "false_positives": 1, "false_negatives": 1, "true_negatives": 2,
    }  # 0.5 >= threshold 0.5 flags  # fmt: skip
    got = {r.name: r for r in result.results}
    assert (got["recall"].numerator, got["recall"].denominator) == (2, 3)
    assert (got["precision"].numerator, got["precision"].denominator) == (2, 3)
    assert (got["false_positive_rate"].numerator, got["false_positive_rate"].denominator) == (1, 3)
    assert (got["accuracy"].numerator, got["accuracy"].denominator) == (4, 6)
    assert got["f1"].value == pytest.approx(2 / 3, abs=1e-6)
    assert got["f1"].low is None  # no interval for F1
    assert all(r.outcome == "pass" for r in result.results)


def test_wilson_matches_the_published_value() -> None:
    low, high = wilson(1, 10, 0.95)  # Newcombe (1998), table I: 0.0179 to 0.4042
    assert (round(low, 4), round(high, 4)) == (0.0179, 0.4042)
    assert wilson(0, 20, 0.95)[0] == 0.0
    assert wilson(20, 20, 0.95)[1] == 1.0
    with pytest.raises(ValueError, match="no trials"):
        wilson(0, 0, 0.95)


def test_roc_auc_counts_ties_as_half() -> None:
    labels = pl.Series([True, True, False, False, False])
    assert roc_auc(labels, pl.Series([0.9, 0.2, 0.8, 0.1, 0.0])) == pytest.approx(5 / 6)
    assert roc_auc(labels, pl.Series([0.5] * 5)) == 0.5
    with pytest.raises(ValueError, match="both positives and negatives"):
        roc_auc(pl.Series([True]), pl.Series([0.3]))


def test_roc_auc_agrees_with_scikit_learn() -> None:
    sklearn = pytest.importorskip("sklearn.metrics")
    data = synthetic(2_000, 200)
    labels = data["is_fraud"] == "true"
    scores = data["score"].cast(pl.Float64)
    assert roc_auc(labels, scores) == pytest.approx(
        sklearn.roc_auc_score(labels.to_list(), scores.to_list()), abs=1e-12
    )


def test_the_auc_interval_narrows_with_more_data() -> None:
    small = auc_interval(0.9, 20, 200, 0.95)
    large = auc_interval(0.9, 2_000, 20_000, 0.95)
    assert small[0] < large[0] < 0.9 < large[1] < small[1]
    assert auc_interval(0.999, 5, 5, 0.95)[1] == 1.0  # clipped


def test_too_few_positives_is_an_error_with_the_numbers_still_reported() -> None:
    data = synthetic(1_000, 4)
    result = evaluate(data, definition({"name": "recall", "at_least": 0.1}, min_positives=30))
    assert result.error == ("too few rows to judge the model: 4 positives, at least 30")
    [recall] = result.results
    assert recall.outcome == "error"
    assert recall.denominator == 4  # still shown, never judged
    assert result.problems == [result.error]


def test_judging_on_the_confidence_bound_is_stricter() -> None:
    data = synthetic(400, 40)
    loose = evaluate(data, definition({"name": "recall", "at_least": 0.8}))
    [recall] = loose.results
    assert recall.value is not None
    assert recall.low is not None
    target = round((recall.value + recall.low) / 2, 4)  # between the bound and the value
    on_value = evaluate(data, definition({"name": "recall", "at_least": target}))
    on_bound = evaluate(data, definition({"name": "recall", "at_least": target},
                                         judge_on="confidence_bound"))  # fmt: skip
    assert on_value.results[0].outcome == "pass"
    assert on_bound.results[0].outcome == "fail"
    assert on_bound.results[0].problem is not None
    assert on_bound.results[0].problem.startswith("lower bound ")
    assert f"of {recall.denominator:,}" in on_bound.results[0].problem


def test_an_at_most_target_fails_above_it() -> None:
    data = frame([0.9, 0.9, 0.9, 0.1], ["false", "false", "true", "false"])
    [fpr] = evaluate(data, definition({"name": "false_positive_rate", "at_most": 0.1})).results
    assert (fpr.numerator, fpr.denominator, fpr.outcome) == (2, 3, "fail")
    assert fpr.problem is not None
    assert fpr.problem.startswith("value 0.6667 is above 0.1")


def test_precision_with_nothing_flagged_is_undefined_not_zero() -> None:
    data = frame([0.1, 0.2, 0.3], ["true", "false", "false"])
    [precision] = evaluate(data, definition({"name": "precision", "at_least": 0.5})).results
    assert precision.value is None
    assert precision.outcome == "error"
    assert precision.error == "undefined: nothing was flagged"


def test_nulls_are_left_out_and_counted() -> None:
    data = frame([0.9, None, 0.1, 0.8], ["true", "true", None, "false"])
    result = evaluate(data, definition({"name": "accuracy", "at_least": 0}))
    assert (result.rows_read, result.null_rows) == (4, 2)
    assert result.confusion is not None
    assert result.confusion.n == 2


@pytest.mark.parametrize(
    ("scores", "labels", "message"),
    [
        (["0.9", "high"], ["true", "false"], "1 score(s) are not finite numbers, e.g. 'high'"),
        (["0.9", "inf"], ["true", "false"], "not finite numbers"),
        (["0.9", "0.1"], ["true", "maybe"], "1 label(s) are not true/false or 1/0, e.g. 'maybe'"),
    ],
)
def test_data_that_is_not_scores_and_labels_is_an_error(
    scores: list[str], labels: list[str], message: str
) -> None:
    result = evaluate(frame(scores, labels), definition({"name": "recall", "at_least": 0}))
    assert result.error is not None
    assert message in result.error
    assert result.confusion is None


def test_a_missing_column_is_named() -> None:
    data = pl.DataFrame({"score": ["0.1"]})
    result = evaluate(data, definition({"name": "recall", "at_least": 0}))
    assert result.error == "column(s) 'is_fraud' not in the data"


def test_columns_and_threshold_are_configurable() -> None:
    data = pl.DataFrame({"p": ["0.3", "0.1"], "y": ["1", "0"]})
    result = evaluate(data, definition({"name": "recall", "at_least": 1}, score_column="p",
                                       label_column="y", threshold=0.25))  # fmt: skip
    assert result.results[0].outcome == "pass"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"metrics": []}, "at least 1"),
        ({"metrics": [{"name": "recall"}]}, "needs at_least or at_most"),
        ({"metrics": [{"name": "recall", "at_least": 0.9, "at_most": 0.1}]}, "above at_most"),
        ({"metrics": [{"name": "recall", "at_least": 0.5}] * 2}, "listed twice"),
        ({"metrics": [{"name": "lift", "at_least": 0.5}]}, "Input should be"),
        ({"metrics": [{"name": "f1", "at_least": 0.5}], "judge_on": "confidence_bound"},
         "f1 has no confidence interval"),
        ({"metrics": [{"name": "recall", "at_least": 0.5}], "confidence": 1.0}, "less than"),
    ],
)  # fmt: skip
def test_a_bad_definition_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MetricDefinition.model_validate(BASE | change)
