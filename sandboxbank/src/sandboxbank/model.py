"""SandboxBank's fraud model (ADR-005): a gradient-boosted classifier on synthetic data.

It exists so RAI has a real model to test: model evaluation with sample sizes
(FR-09.1) and drift of its scores (FR-09.2). It is not a fraud model for any real
institution, and its numbers say nothing about real fraud.

**No model file is shipped.** Loading a pickled model can run arbitrary code, so the
model is defined by its **recipe** (development seeds, batch sizes, features and
hyperparameters, all below) and retrained from it in seconds whenever it is needed.
The same recipe and the same scikit-learn version give the same model; the model
card records the recipe's SHA-256, the versions, and a fingerprint of the scores on
the hold-out batches, so anyone can rebuild it and check.

**Features** are what the feed carries, joined with the two reference tables the
contracts already use (accounts, merchants): amount, amount over the account's
transaction limit, channel, direction, status, account type, whether a merchant is
named, and the merchant's category. The label ``is_fraud`` is the target only. (The
hour of day is left out: SandboxBank's batches are 15-minute windows from one start,
so it carries nothing.) Categorical values have fixed vocabularies from the generator; a value
outside them is treated as missing.

Requires the ``model`` extra: ``uv sync --package rai-sandboxbank --extra model``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sandboxbank import __version__
from sandboxbank.generator import (
    ACCOUNT_TYPES,
    CHANNELS_BY_ACCOUNT_TYPE,
    MCC_CATEGORIES,
    TXN_STATUS,
    GeneratorConfig,
    generate_accounts,
    generate_batch,
    generate_merchants,
)

FORMAT = "sandboxbank-fraud-model/1"
CHANNELS = tuple(sorted({c for options in CHANNELS_BY_ACCOUNT_TYPE.values() for c, _ in options}))
STATUSES = tuple(s for s, _ in TXN_STATUS)
ACCOUNT_KINDS = tuple(a for a, _ in ACCOUNT_TYPES)
CATEGORIES = tuple(sorted({category for _, category, _ in MCC_CATEGORIES}))
FEATURES: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("amount", None),
    ("amount_to_limit", None),
    ("channel", CHANNELS),
    ("credit", None),
    ("status", STATUSES),
    ("account_type", ACCOUNT_KINDS),
    ("has_merchant", None),
    ("merchant_category", CATEGORIES),
)  # (name, vocabulary for a categorical feature)

RECIPE: dict[str, Any] = {
    "format": FORMAT,
    "data_seed": 31_000,  # development range: never an evaluation seed
    "accounts": 5_000,
    "merchants": 500,
    "rows_per_batch": 50_000,
    "train_batches": [0, 1, 2, 3, 4, 5, 6, 7],
    "holdout_batches": [8, 9],
    "features": [name for name, _ in FEATURES],
    # Conservative on purpose: about 800 fraud rows train it, and a faster learning rate
    # with small leaves was unstable (hold-out ROC AUC 0.65, training 0.68).
    "estimator": {
        "learning_rate": 0.05, "max_iter": 100, "max_leaf_nodes": 15, "min_samples_leaf": 200,
        "l2_regularization": 1.0, "early_stopping": False, "random_state": 0,
    },
    "threshold": 0.5,
}  # fmt: skip


def recipe_sha256(recipe: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Reference:
    """The two reference tables, keyed for the join."""

    accounts: dict[str, tuple[str, float]]  # account_id -> (account_type, txn_limit in dollars)
    merchants: dict[str, str]  # merchant_id -> category

    @classmethod
    def from_rows(
        cls, accounts: Iterable[Mapping[str, str]], merchants: Iterable[Mapping[str, str]]
    ) -> Reference:
        """From the feed's ``accounts.csv`` and ``merchants.csv`` rows."""
        return cls(
            {a["account_id"]: (a["account_type"], float(a["txn_limit"])) for a in accounts},
            {m["merchant_id"]: m["category"] for m in merchants},
        )


def _code(value: str | None, vocabulary: tuple[str, ...]) -> float:
    return float(vocabulary.index(value)) if value in vocabulary else math.nan


Value = float | str | None
CATEGORICAL = {name for name, vocabulary in FEATURES if vocabulary is not None}


def feature_record(row: Mapping[str, str], reference: Reference) -> dict[str, Value]:
    """One transaction (a ``transactions_raw`` row, as text) as the model's named inputs:
    numbers for numeric features, the plain value for categorical ones, None if missing.
    This is what ``sandboxbank model features`` writes and the scoring service takes."""
    amount = float(row["amount"])
    kind, limit = reference.accounts.get(row["account_id"], (None, math.nan))
    merchant = row.get("merchant_id") or None
    return {
        "amount": amount,
        "amount_to_limit": amount / limit if limit and not math.isnan(limit) else None,
        "channel": row["channel"] or None,
        "credit": 1.0 if row["direction"] == "credit" else 0.0,
        "status": row["status"] or None,
        "account_type": kind,
        "has_merchant": 0.0 if merchant is None else 1.0,
        "merchant_category": reference.merchants.get(merchant) if merchant else None,
    }


def encode(record: Mapping[str, Value]) -> list[float]:
    """Named inputs as the classifier's row. A missing or non-numeric number, and a
    category outside its vocabulary, become NaN: the model treats both as missing."""
    encoded = []
    for name, vocabulary in FEATURES:
        value = record.get(name)
        if vocabulary is not None:
            encoded.append(_code(value if isinstance(value, str) else None, vocabulary))
        elif isinstance(value, bool) or not isinstance(value, int | float):
            encoded.append(math.nan)
        else:
            encoded.append(float(value) if math.isfinite(value) else math.nan)
    return encoded


def features(row: Mapping[str, str], reference: Reference) -> list[float]:
    """One transaction (a ``transactions_raw`` row, as text) as the classifier's row."""
    return encode(feature_record(row, reference))


def _generated(recipe: Mapping[str, Any], batches: Sequence[int]) -> tuple[
    list[dict[str, str]], Reference
]:  # fmt: skip
    config = GeneratorConfig(
        seed=recipe["data_seed"], accounts=recipe["accounts"], merchants=recipe["merchants"],
        batches=max(batches) + 1, rows_per_batch=recipe["rows_per_batch"],
    )  # fmt: skip
    accounts, merchants = generate_accounts(config), generate_merchants(config)
    header = ("txn_id", "batch_id", "account_id", "merchant_id", "channel", "direction",
              "amount", "currency", "status", "created_at", "is_fraud")  # fmt: skip
    rows = [
        dict(zip(header, txn.as_row(), strict=True))
        for index in batches
        for txn in generate_batch(config, accounts, merchants, index)
    ]
    reference = Reference(
        {a.account_id: (a.account_type, a.txn_limit_cents / 100) for a in accounts},
        {m.merchant_id: m.category for m in merchants},
    )
    return rows, reference


@dataclass(frozen=True)
class Model:
    estimator: Any  # sklearn HistGradientBoostingClassifier
    recipe: dict[str, Any]

    def score(self, rows: Sequence[Mapping[str, str]], reference: Reference) -> list[float]:
        """The probability of fraud for each row, in order."""
        if not rows:
            return []
        import numpy as np  # comes with scikit-learn

        matrix = np.asarray([features(row, reference) for row in rows], dtype=float)
        return [float(p) for p in self.estimator.predict_proba(matrix)[:, 1]]

    def score_records(self, records: Sequence[Mapping[str, Value]]) -> list[float]:
        """The probability of fraud for each named-input record (``feature_record``)."""
        if not records:
            return []
        import numpy as np  # comes with scikit-learn

        matrix = np.asarray([encode(record) for record in records], dtype=float)
        return [float(p) for p in self.estimator.predict_proba(matrix)[:, 1]]

    @property
    def identity(self) -> dict[str, str]:
        """Which model this is: its format, recipe hash and scikit-learn version."""
        import sklearn

        return {"format": FORMAT, "recipe_sha256": recipe_sha256(self.recipe),
                "scikit_learn": sklearn.__version__, "sandboxbank": __version__}  # fmt: skip


def train(recipe: Mapping[str, Any] | None = None) -> Model:
    """Generate the training batches and fit the classifier (``RECIPE`` by default).
    Deterministic for a recipe and a scikit-learn version."""
    recipe = RECIPE if recipe is None else recipe
    import numpy as np  # comes with scikit-learn
    from sklearn.ensemble import HistGradientBoostingClassifier

    rows, reference = _generated(recipe, recipe["train_batches"])
    categorical = [vocabulary is not None for _, vocabulary in FEATURES]
    estimator = HistGradientBoostingClassifier(
        categorical_features=categorical, **recipe["estimator"]
    )
    matrix = np.asarray([features(r, reference) for r in rows], dtype=float)
    estimator.fit(matrix, np.asarray([r["is_fraud"] == "true" for r in rows]))
    return Model(estimator, dict(recipe))


@dataclass(frozen=True)
class Evaluation:
    """Metrics at a threshold, each with what it counts (FR-09.1: n always)."""

    n: int
    positives: int
    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    roc_auc: float | None
    average_precision: float | None

    @property
    def precision(self) -> float | None:
        flagged = self.true_positives + self.false_positives
        return self.true_positives / flagged if flagged else None

    @property
    def recall(self) -> float | None:
        return self.true_positives / self.positives if self.positives else None


def evaluate(labels: Sequence[bool], scores: Sequence[float], threshold: float) -> Evaluation:
    from sklearn.metrics import average_precision_score, roc_auc_score

    if len(labels) != len(scores):
        raise ValueError("labels and scores differ in length")
    flagged = [s >= threshold for s in scores]
    pairs = list(zip(labels, flagged, strict=True))
    both = 0 < sum(labels) < len(labels)  # AUCs need both classes
    return Evaluation(
        n=len(labels), positives=sum(labels), threshold=threshold,
        true_positives=sum(1 for y, f in pairs if y and f),
        false_positives=sum(1 for y, f in pairs if not y and f),
        false_negatives=sum(1 for y, f in pairs if y and not f),
        true_negatives=sum(1 for y, f in pairs if not y and not f),
        roc_auc=float(roc_auc_score(labels, scores)) if both else None,
        average_precision=float(average_precision_score(labels, scores)) if both else None,
    )  # fmt: skip


def holdout(model: Model) -> tuple[Evaluation, str]:
    """The model card's numbers: the recipe's hold-out batches, and a fingerprint of
    their scores (SHA-256 of the scores rounded to 6 places)."""
    rows, reference = _generated(model.recipe, model.recipe["holdout_batches"])
    scores = model.score(rows, reference)
    labels = [r["is_fraud"] == "true" for r in rows]
    fingerprint = hashlib.sha256(",".join(f"{s:.6f}" for s in scores).encode()).hexdigest()
    return evaluate(labels, scores, model.recipe["threshold"]), fingerprint


def card(model: Model, evaluation: Evaluation, fingerprint: str) -> str:
    """The model card (ADR-005), as Markdown. Every number is computed here."""
    import sklearn

    recipe = model.recipe

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{100 * value:.1f}%"

    def num(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.4f}"

    e = evaluation
    rate = e.positives / e.n if e.n else None
    flagged = e.true_positives + e.false_positives
    train_rows = recipe["rows_per_batch"] * len(recipe["train_batches"])
    lines = [
        "# Model card: SandboxBank fraud model",
        "",
        "> Generated by `sandboxbank model card`; do not edit by hand. **Synthetic data "
        "only**: this model exists so RAI has a real model to test (ADR-005). It is not a "
        "fraud model for any real institution, and its numbers say nothing about real fraud.",
        "",
        "| | |",
        "| --- | --- |",
        f"| Model | scikit-learn `HistGradientBoostingClassifier` ({recipe['format']}) |",
        f"| Versions | scikit-learn {sklearn.__version__}, rai-sandboxbank {__version__} |",
        f"| Recipe SHA-256 | `{recipe_sha256(recipe)}` |",
        f"| Hold-out score fingerprint | `{fingerprint}` (this platform) |",
        "| Task | Probability that a transaction is fraud (`is_fraud`) |",
        "",
        "## Data",
        "",
        f"- **Training:** {train_rows:,} transactions, SandboxBank generator, development seed "
        f"{recipe['data_seed']}, batches {recipe['train_batches']}, "
        f"{recipe['accounts']:,} accounts and {recipe['merchants']:,} merchants.",
        f"- **Hold-out:** batches {recipe['holdout_batches']} of the same seed, never seen in "
        "training. Evaluation seeds (100,000 to 199,999) are never used: they belong to the "
        "registered benchmark.",
        "- **Fraud** is the generator's: about 0.2% of transactions, with their own channel "
        "mix, larger amounts, more declines and more high-risk merchant categories. The "
        "model learns these rules back; real fraud is not like this.",
        "",
        "## Features",
        "",
        ", ".join(f"`{name}`" + (" (categorical)" if vocab else "") for name, vocab in FEATURES)
        + ". The label is the target only.",
        "",
        f"## Hold-out performance (threshold {e.threshold})",
        "",
        "| Metric | Value | Counts |",
        "| --- | --- | --- |",
        f"| Transactions | {e.n:,} | {e.positives:,} fraud ({pct(rate)}) |",
        f"| ROC AUC | {num(e.roc_auc)} | over all {e.n:,} |",
        f"| Average precision | {num(e.average_precision)} | over all {e.n:,} |",
        f"| Precision | {pct(e.precision)} | {e.true_positives:,} of {flagged:,} flagged |",
        f"| Recall | {pct(e.recall)} | {e.true_positives:,} of {e.positives:,} fraud |",
        f"| False positives | {e.false_positives:,} | of {e.n - e.positives:,} legitimate |",
        "",
        "## Rebuild and check",
        "",
        "```bash",
        "uv sync --package rai-sandboxbank --extra model",
        "uv run sandboxbank model card --out sandboxbank/MODEL_CARD.md   # retrains, re-scores",
        "uv run sandboxbank model score --feed <feed folder> --batch <file> --out scores.csv",
        "```",
        "",
        "The same recipe and scikit-learn version give the same model. The fingerprint can "
        "differ across platforms in the last digits of floating point; the recipe SHA-256 "
        "cannot.",
        "",
        "## Limitations",
        "",
        "- Trained and tested on one synthetic generator; it has never seen real data.",
        "- No fairness analysis: the synthetic data has no protected attributes.",
        "- Not calibrated beyond what gradient boosting gives; scores rank, they are not "
        "probabilities to act on.",
        "",
    ]
    return "\n".join(lines)
