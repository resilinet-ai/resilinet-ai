"""Robustness tests (FR-09.3): RAI perturbs the inputs itself, seeded and recorded, has
every variant scored by the system under test's model, and judges the degradation of
each metric with its n."""

from __future__ import annotations

import math
from typing import Any

import httpx
import polars as pl
import pytest
from pydantic import ValidationError

from rai.tevv.robustness import (
    RobustnessDefinition,
    Scorer,
    describe_variant,
    inputs,
    normal,
    perturb,
    robustness,
    uniform,
)
from rai.worker.scoring import http_scorer

MODEL = {"format": "toy/1", "recipe_sha256": "abc"}


def table(n: int = 2_000, positives: int = 200) -> pl.DataFrame:
    """Fraud is large amounts on the 'web' channel; the label is exact."""
    rows = []
    for i in range(n):
        fraud = i < positives
        rows.append({
            "txn_id": f"T{i:05d}",
            "amount": f"{(900 + i % 50) if fraud else (10 + i % 300)}.00",
            "channel": "web" if fraud or i % 7 == 0 else "pos",
            "is_fraud": "true" if fraud else "false",
        })  # fmt: skip
    return pl.DataFrame(rows)


def toy_scorer(identity: dict[str, Any] | None = None) -> Scorer:
    """Scores high when the amount is large and the channel is 'web'; missing or unknown
    inputs score low, as a real model would lose the signal."""

    def score(rows: list[dict[str, float | str | None]]) -> tuple[list[float], dict[str, Any]]:
        out = []
        for row in rows:
            amount, channel = row["amount"], row["channel"]
            big = isinstance(amount, float) and amount > 500
            out.append(0.9 if big and channel == "web" else (0.4 if big else 0.1))
        return out, identity or MODEL

    return score


def definition(**extra: Any) -> RobustnessDefinition:
    body: dict[str, Any] = {
        "connector": "feed", "environment": "SANDBOX-01", "scorer": "model-api",
        "features": ["amount", "channel"], "categorical": ["channel"], "seed": 7,
        "perturbations": [
            {"name": "amount-noise", "kind": "noise", "columns": ["amount"], "scale": 0.05},
            {"name": "no-channel", "kind": "missing", "columns": ["channel"]},
            {"name": "odd-channel", "kind": "unknown_category", "columns": ["channel"],
             "share": 0.5},
        ],
        "metrics": [{"name": "recall", "max_drop": 0.1},
                    {"name": "false_positive_rate", "max_drop": 0.05}],
    }  # fmt: skip
    return RobustnessDefinition.model_validate(body | extra)


def test_draws_are_reproducible_and_look_random() -> None:
    assert uniform(7, "a", 1) == uniform(7, "a", 1)
    assert uniform(7, "a", 1) != uniform(8, "a", 1)
    draws = [uniform(1, "x", i) for i in range(10_000)]
    assert all(0 <= d < 1 for d in draws)
    assert abs(sum(draws) / len(draws) - 0.5) < 0.02
    normals = [normal(1, "x", i) for i in range(10_000)]
    mean = sum(normals) / len(normals)
    sd = math.sqrt(sum((z - mean) ** 2 for z in normals) / len(normals))
    assert abs(mean) < 0.05
    assert abs(sd - 1) < 0.05


def test_a_variant_is_the_same_every_time_and_records_what_changed() -> None:
    typed, _ = inputs(table(), definition())
    noise, missing, odd = definition().perturbations
    first, rows, cells = perturb(typed, odd, 7)
    again, *_ = perturb(typed, odd, 7)
    assert first.equals(again)
    assert 900 < rows < 1_100  # half the rows
    assert cells == rows
    assert first.filter(pl.col("channel") == "__unknown__").height == rows

    gone, rows, cells = perturb(typed, missing, 7)
    assert (rows, cells, gone["channel"].null_count()) == (2_000, 2_000, 2_000)

    noisy, rows, _ = perturb(typed, noise, 7)
    ratio = (noisy["amount"] / typed["amount"] - 1).abs()
    assert rows == 2_000
    assert 0 < ratio.max() < 0.3  # 5% noise, within six standard deviations
    assert noisy["channel"].equals(typed["channel"])  # only the named column changes


def test_noise_leaves_a_missing_number_missing() -> None:
    frame = pl.DataFrame({"amount": [1.0, None, 3.0]})
    noise = definition().perturbations[0]
    out, rows, cells = perturb(frame, noise, 7)
    assert out["amount"][1] is None
    assert (rows, cells) == (3, 2)


def test_degradation_is_judged_per_metric_with_its_n() -> None:
    result = robustness(table(), definition(), toy_scorer())
    assert result.error is None
    assert result.model == MODEL
    assert result.rows_used == 2_000
    by_name = {v.name: v for v in result.variants}
    assert list(by_name) == ["clean", "amount-noise", "no-channel", "odd-channel"]
    assert by_name["clean"].outcome == "pass"
    assert by_name["amount-noise"].outcome == "pass"  # the toy model only needs > 500
    lost = by_name["no-channel"]
    assert lost.outcome == "fail"
    recall = next(d for d in lost.degradations if d.name == "recall")
    assert (recall.clean, recall.value, recall.numerator, recall.denominator) == (1.0, 0.0, 0, 200)
    assert recall.degradation == 1.0
    assert recall.low is not None
    assert recall.high is not None
    assert "degraded by 1.0000, more than 0.1" in (recall.problem or "")
    described = describe_variant(lost)
    assert described["rows_perturbed"] == 2_000
    assert described["perturbation"]["kind"] == "missing"
    assert described["metrics"]["recall"]["denominator"] == 200
    assert len(described["inputs_digest"]) == 64
    assert any(p.startswith("no-channel: recall degraded") for p in result.problems)


def test_a_rising_false_positive_rate_is_a_degradation() -> None:
    def alarmed(rows: list[dict[str, Any]]) -> tuple[list[float], dict[str, Any]]:
        return [0.9 if r["channel"] in ("web", "__unknown__") else 0.1 for r in rows], MODEL

    result = robustness(table(), definition(), alarmed)
    odd = next(v for v in result.variants if v.name == "odd-channel")
    fpr = next(d for d in odd.degradations if d.name == "false_positive_rate")
    assert fpr.value is not None
    assert fpr.clean is not None
    assert fpr.value > fpr.clean
    assert fpr.degradation == pytest.approx(fpr.value - fpr.clean, abs=1e-6)
    assert fpr.problem is not None


def test_the_same_definition_and_table_give_the_same_evidence() -> None:
    one = robustness(table(), definition(), toy_scorer())
    two = robustness(table(), definition(), toy_scorer())
    assert [describe_variant(v) for v in one.variants] == [
        describe_variant(v) for v in two.variants
    ]
    other = robustness(table(), definition(seed=8), toy_scorer())
    assert (
        describe_variant(one.variants[3])["inputs_digest"]
        != describe_variant(other.variants[3])["inputs_digest"]
    )


def test_a_model_that_changes_between_calls_is_an_error() -> None:
    calls = iter(range(100))

    def drifting(rows: list[dict[str, Any]]) -> tuple[list[float], dict[str, Any]]:
        scores, _ = toy_scorer()(rows)
        return scores, {"recipe_sha256": "abc" if next(calls) < 2 else "def"}

    result = robustness(table(), definition(), drifting)
    assert [v.outcome for v in result.variants] == ["pass", "error", "error", "error"]
    assert "the model changed" in (result.variants[1].error or "")


@pytest.mark.parametrize(
    ("scorer_answer", "message"),
    [((lambda rows: ([0.5], MODEL)), "returned 1 scores"),
     ((lambda rows: ([math.nan] * len(rows), MODEL)), "not a finite number")],
)  # fmt: skip
def test_a_scorer_that_answers_wrongly_fails_closed(scorer_answer: Scorer, message: str) -> None:
    result = robustness(table(), definition(), scorer_answer)
    assert result.error is not None
    assert message in result.error


def test_too_little_data_is_an_error_not_a_pass() -> None:
    result = robustness(table(200, 10), definition(), toy_scorer())
    assert all(v.outcome == "error" for v in result.variants)
    assert "too few rows" in result.problems[0]


def test_the_data_must_be_what_the_test_says() -> None:
    no_column = robustness(table().drop("channel"), definition(), toy_scorer())
    assert no_column.error == "column(s) 'channel' not in the data"
    text = table().with_columns(pl.lit("lots").alias("amount"))
    assert "amount is numeric" in (robustness(text, definition(), toy_scorer()).error or "")
    label = table().with_columns(pl.lit("maybe").alias("is_fraud"))
    assert "not true/false" in (robustness(label, definition(), toy_scorer()).error or "")
    capped = robustness(table(), definition(max_rows=1_000), toy_scorer())
    assert "at most 1,000 are scored" in (capped.error or "")


def test_rows_without_a_label_are_left_out_and_counted() -> None:
    frame = table().with_columns(
        pl.when(pl.col("txn_id") == "T01999").then(None).otherwise(pl.col("is_fraud"))
        .alias("is_fraud")
    )  # fmt: skip
    result = robustness(frame, definition(), toy_scorer())
    assert (result.rows_read, result.rows_used, result.null_label_rows) == (2_000, 1_999, 1)


@pytest.mark.parametrize(
    ("change", "message"),
    [({"categorical": ["merchant"]}, "categorical column must be one of the features"),
     ({"features": ["amount", "is_fraud"], "categorical": []}, "not features"),
     ({"perturbations": [{"name": "n", "kind": "noise", "columns": ["channel"], "scale": 0.1}]},
      "numeric features only"),
     ({"perturbations": [{"name": "u", "kind": "unknown_category", "columns": ["amount"]}]},
      "categorical features"),
     ({"perturbations": [{"name": "n", "kind": "noise", "columns": ["amount"]}]}, "needs a scale"),
     ({"perturbations": [{"name": "m", "kind": "missing", "columns": ["amount"], "scale": 1}]},
      "only noise has a scale"),
     ({"perturbations": [{"name": "clean", "kind": "missing", "columns": ["amount"]}]},
      "not 'clean'"),
     ({"perturbations": [{"name": "m", "kind": "missing", "columns": ["amonut"]}]},
      "are not features"),
     ({"metrics": [{"name": "recall", "max_drop": 0.1}, {"name": "recall", "max_drop": 0.2}]},
      "listed twice")],
)  # fmt: skip
def test_a_definition_that_cannot_work_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        definition(**change)


def test_the_http_scorer_sends_only_features_and_fails_closed() -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read())
        if b"broken" in request.content:
            return httpx.Response(503, text="the model is still training")
        if b"odd" in request.content:
            return httpx.Response(200, json={"nothing": True})
        return httpx.Response(200, json={"model": MODEL, "scores": [0.25]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    score = http_scorer("http://model/v1/score", client)
    assert score([{"amount": 1.0, "channel": "web"}]) == ([0.25], MODEL)
    assert seen[0] == b'{"rows":[{"amount":1.0,"channel":"web"}]}'
    with pytest.raises(ValueError, match="answered 503"):
        score([{"amount": 1.0, "channel": "broken"}])
    with pytest.raises(ValueError, match="not \\{scores, model\\}"):
        score([{"amount": 1.0, "channel": "odd"}])

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    unreachable = http_scorer(
        "http://model/v1/score", httpx.Client(transport=httpx.MockTransport(down))
    )
    with pytest.raises(ValueError, match="did not answer"):
        unreachable([{"amount": 1.0, "channel": "web"}])
