"""PSI and KS drift (FR-09.2): the statistics, their sample sizes and their alerts."""

from __future__ import annotations

import math
import random

import polars as pl
import pytest
from pydantic import ValidationError

from rai.tevv.drift import (
    DriftColumn,
    DriftDefinition,
    column_drift,
    drift,
    ks_p_value,
    ks_statistic,
    psi,
)


def normal(n: int, mean: float = 0.0, sd: float = 1.0, seed: int = 1) -> list[float]:
    rng = random.Random(seed)  # noqa: S311 - test data
    return [rng.gauss(mean, sd) for _ in range(n)]


def frame(values: list[object], name: str = "x") -> pl.DataFrame:
    return pl.DataFrame({name: values}, strict=False)


def test_psi_by_hand() -> None:
    assert psi([0.5, 0.5], [0.5, 0.5]) == 0.0
    expected = (0.7 - 0.5) * math.log(0.7 / 0.5) + (0.3 - 0.5) * math.log(0.3 / 0.5)
    assert psi([0.5, 0.5], [0.7, 0.3]) == pytest.approx(expected)
    assert math.isfinite(psi([1.0, 0.0], [0.0, 1.0]))  # empty bins stay finite


def test_ks_by_hand_with_ties() -> None:
    assert ks_statistic(pl.Series([1, 2, 3]), pl.Series([1, 2, 3])) == 0.0
    assert ks_statistic(pl.Series([1, 2]), pl.Series([3, 4])) == 1.0
    # CDFs at 1: 2/3 vs 0; at 2: 1 vs 1/2; at 3: 1 vs 1 -> D = 2/3
    assert ks_statistic(pl.Series([1, 1, 2]), pl.Series([2, 3])) == pytest.approx(2 / 3)
    assert ks_p_value(0.0, 10, 10) == 1.0


def test_ks_matches_scipy() -> None:
    stats = pytest.importorskip("scipy.stats")
    for shift in (0.0, 0.05, 0.3):
        a, b = normal(2_000, seed=1), normal(1_500, mean=shift, seed=2)
        d = ks_statistic(pl.Series(a), pl.Series(b))
        reference = stats.ks_2samp(a, b, method="asymp")
        assert d == pytest.approx(reference.statistic, abs=1e-12)
        assert ks_p_value(d, len(a), len(b)) == pytest.approx(reference.pvalue, rel=0.05, abs=1e-6)


def column(**settings: object) -> DriftColumn:
    return DriftColumn.model_validate({"name": "x", **settings})


def test_the_same_distribution_does_not_alert() -> None:
    result = column_drift(frame(normal(5_000, seed=1)), frame(normal(5_000, seed=2)), column(),
                          bins=10, min_rows=100)  # fmt: skip
    assert result.outcome == "pass"
    assert result.psi is not None
    assert result.psi < 0.02
    assert result.band == "stable"
    assert (result.reference_rows, result.current_rows) == (5_000, 5_000)
    assert len(result.bins) == 10
    assert sum(b["reference_share"] for b in result.bins) == pytest.approx(1.0)
    assert all(b["reference_share"] == pytest.approx(0.1, abs=0.001) for b in result.bins)


def test_a_shift_alerts_on_psi_and_ks() -> None:
    result = column_drift(frame(normal(5_000)), frame(normal(5_000, mean=1.0, seed=3)),
                          column(), bins=10, min_rows=100)  # fmt: skip
    assert result.outcome == "fail"
    assert result.band == "significant shift"
    assert [a.split()[0] for a in result.alerts] == ["PSI", "KS"]
    assert result.ks_p_value is not None
    assert result.ks_p_value < 1e-9


def test_thresholds_are_the_definitions() -> None:
    small = (frame(normal(5_000)), frame(normal(5_000, mean=0.15, seed=3)))
    loose = column_drift(*small, column(), bins=10, min_rows=100)
    strict = column_drift(*small, column(psi_alert=0.01, ks_alert=0.01), bins=10, min_rows=100)
    psi_only = column_drift(*small, column(psi_alert=5, ks_alert=None), bins=10, min_rows=100)
    assert (loose.outcome, strict.outcome, psi_only.outcome) == ("pass", "fail", "pass")
    assert psi_only.ks_statistic is not None  # still reported


def test_categorical_columns_use_psi_over_categories() -> None:
    before = frame(["card"] * 700 + ["transfer"] * 300, "channel")
    after = frame(["card"] * 400 + ["transfer"] * 400 + ["crypto"] * 200, "channel")
    spec = DriftColumn(name="channel", kind="categorical")
    result = column_drift(before, after, spec, bins=10, min_rows=100)
    assert result.outcome == "fail"
    assert result.ks_statistic is None
    assert [b["category"] for b in result.bins] == ["card", "crypto", "transfer"]
    same = column_drift(before, before, spec, bins=10, min_rows=100)
    assert (same.psi, same.outcome) == (0.0, "pass")


def test_nulls_are_left_out_and_reported() -> None:
    values: list[object] = [*normal(900), *([None] * 100)]
    result = column_drift(frame(values), frame(normal(1_000, seed=2)), column(), bins=10,
                          min_rows=100)  # fmt: skip
    assert (result.reference_rows, result.reference_null_share) == (900, 0.1)
    assert result.current_null_share == 0.0


@pytest.mark.parametrize(
    ("reference", "current", "error"),
    [
        (frame(normal(50)), frame(normal(5_000)), "too few values"),
        (frame(normal(500)), frame(normal(500), "y"), "not in the current data"),
        (frame(["a"] * 500), frame(["b"] * 500), "not numeric"),
    ],
)
def test_what_cannot_be_judged_is_an_error_not_a_pass(
    reference: pl.DataFrame, current: pl.DataFrame, error: str
) -> None:
    result = column_drift(reference, current, column(), bins=10, min_rows=100)
    assert result.outcome == "error"
    assert result.error is not None
    assert error in result.error


def test_many_ties_keep_bins_valid() -> None:
    """A column with few distinct values gives repeated quantiles: fewer, wider bins."""
    values = [1.0] * 800 + [2.0] * 200
    result = column_drift(frame(values), frame(values), column(), bins=10, min_rows=100)
    assert result.outcome == "pass"
    assert [b["to"] for b in result.bins] == [1.0, None]  # values 1 | 2, no empty bin
    assert sum(b["current_share"] for b in result.bins) == pytest.approx(1.0)


def test_definitions_are_checked() -> None:
    columns = [{"name": "amount"}, {"name": "channel", "kind": "categorical"}]
    good = {"connector": "feed", "environment": "SANDBOX-01",
            "reference": {"asset": "batch_0", "connector": "feed"}, "columns": columns}  # fmt: skip
    definition = DriftDefinition.model_validate(good)
    assert (definition.bins, definition.min_rows) == (10, 100)
    for bad in ({**good, "columns": []}, {**good, "columns": [{"name": "a"}, {"name": "a"}]},
                {**good, "bins": 1}, {**good, "extra": 1}):  # fmt: skip
        with pytest.raises(ValidationError):
            DriftDefinition.model_validate(bad)
    both = drift(frame(normal(500), "amount").with_columns(channel=pl.lit("card")),
                 frame(normal(500, seed=2), "amount").with_columns(channel=pl.lit("card")),
                 definition)  # fmt: skip
    assert [r.column for r in both] == ["amount", "channel"]
