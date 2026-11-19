"""SandboxBank's fraud model (ADR-005): features, training, scoring, evaluation and the
model card. A small recipe keeps the tests quick; the card's recipe is checked too."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sklearn")

from sandboxbank import cli, model
from sandboxbank.fixtures import build_slice
from sandboxbank.generator import seed_purpose

ROOT = Path(__file__).resolve().parents[2]
SMALL: dict[str, Any] = {
    **model.RECIPE, "rows_per_batch": 20_000, "train_batches": [0, 1, 2, 3],
    "holdout_batches": [4],
}  # fmt: skip
REFERENCE = model.Reference({"ACC-1": ("checking", 5000.0)}, {"MER-1": "electronics"})
ROW = {"txn_id": "t", "account_id": "ACC-1", "merchant_id": "MER-1", "channel": "ecommerce",
       "direction": "debit", "amount": "1250.00", "status": "declined",
       "created_at": "2026-01-01T02:00:00.000Z", "is_fraud": "true"}  # fmt: skip


@pytest.fixture(scope="module")
def small() -> model.Model:
    return model.train(SMALL)


def test_features_are_what_the_feed_carries() -> None:
    names = [name for name, _ in model.FEATURES]
    assert "is_fraud" not in names  # the label is the target only
    values = dict(zip(names, model.features(ROW, REFERENCE), strict=True))
    assert values["amount"] == 1250.0
    assert values["amount_to_limit"] == 0.25
    assert values["channel"] == model.CHANNELS.index("ecommerce")
    assert (values["credit"], values["has_merchant"]) == (0.0, 1.0)
    assert values["status"] == model.STATUSES.index("declined")
    assert values["merchant_category"] == model.CATEGORIES.index("electronics")


def test_unknown_values_are_missing_not_guessed() -> None:
    odd = {**ROW, "account_id": "ACC-404", "merchant_id": "", "channel": "carrier-pigeon"}
    values = dict(zip([n for n, _ in model.FEATURES], model.features(odd, REFERENCE), strict=True))
    assert math.isnan(values["amount_to_limit"])
    assert math.isnan(values["channel"])
    assert math.isnan(values["account_type"])
    assert values["has_merchant"] == 0.0
    assert math.isnan(values["merchant_category"])


def test_the_recipe_uses_development_seeds_only() -> None:
    assert seed_purpose(model.RECIPE["data_seed"]) == "development"
    assert not set(model.RECIPE["train_batches"]) & set(model.RECIPE["holdout_batches"])
    assert model.recipe_sha256(model.RECIPE) == model.recipe_sha256(dict(model.RECIPE))
    assert model.recipe_sha256(SMALL) != model.recipe_sha256(model.RECIPE)


def test_training_is_deterministic_and_ranks_fraud_first(small: model.Model) -> None:
    again = model.train(SMALL)
    evaluation, fingerprint = model.holdout(small)
    assert model.holdout(again)[1] == fingerprint  # same recipe, same model
    assert evaluation.n == 20_000
    assert evaluation.positives > 10
    assert evaluation.roc_auc is not None
    assert evaluation.roc_auc > 0.9
    assert evaluation.true_positives + evaluation.false_negatives == evaluation.positives
    assert sum((evaluation.true_positives, evaluation.false_positives,
                evaluation.false_negatives, evaluation.true_negatives)) == evaluation.n  # fmt: skip


def test_evaluation_counts_by_hand() -> None:
    e = model.evaluate([True, True, False, False, False], [0.9, 0.2, 0.8, 0.1, 0.0], 0.5)
    assert (e.true_positives, e.false_negatives, e.false_positives, e.true_negatives) == (
        1,
        1,
        1,
        2,
    )
    assert (e.precision, e.recall) == (0.5, 0.5)
    assert e.roc_auc == pytest.approx(5 / 6)
    one_class = model.evaluate([False, False], [0.1, 0.2], 0.5)
    assert (one_class.roc_auc, one_class.recall, one_class.precision) == (None, None, None)
    with pytest.raises(ValueError, match="differ in length"):
        model.evaluate([True], [0.1, 0.2], 0.5)


def test_the_card_states_every_number_with_its_counts(small: model.Model) -> None:
    evaluation, fingerprint = model.holdout(small)
    text = model.card(small, evaluation, fingerprint)
    assert model.recipe_sha256(SMALL) in text
    assert fingerprint in text
    assert f"{evaluation.positives:,} fraud" in text
    assert f"of {evaluation.positives:,} fraud" in text  # recall with its denominator
    assert "Synthetic data only" in text


def test_the_published_card_matches_the_recipe() -> None:
    """Changing the recipe means regenerating MODEL_CARD.md (sandboxbank model card)."""
    published = (ROOT / "sandboxbank" / "MODEL_CARD.md").read_text(encoding="utf-8")
    assert model.recipe_sha256(model.RECIPE) in published


def test_the_cli_scores_a_feed(
    small: model.Model, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    build_slice(tmp_path / "slice", data_seed=27, injector_seed=28, accounts=200, merchants=40,
                rows_per_batch=2_000)  # fmt: skip
    monkeypatch.setattr(model, "train", lambda recipe=None: small)
    out = tmp_path / "scores.csv"
    feed = str(tmp_path / "slice" / "feed")
    assert (
        cli.main(["model", "score", "--feed", feed, "--batch", "B000000", "--out", str(out)]) == 0
    )
    with out.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == ["txn_id", "score", "is_fraud"]
    assert len(rows) == 2_000
    assert all(0.0 <= float(r["score"]) <= 1.0 for r in rows)
    assert "2,000 transactions scored" in capsys.readouterr().out
    missing = cli.main(["model", "score", "--feed", feed, "--batch", "B999999", "--out", str(out)])
    assert missing == 2

    card = tmp_path / "card.md"
    assert cli.main(["model", "card", "--out", str(card)]) == 0
    assert "Hold-out performance" in card.read_text(encoding="utf-8")
