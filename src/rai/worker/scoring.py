"""Scoring through a model's endpoint, for robustness tests (FR-09.3).

The URL is an allow-listed target's ``score_url``, registered by an admin (B-22); a
test case names the target, never an address. The endpoint takes
``{"rows": [{feature: value or null}]}`` and answers ``{"model": {...}, "scores": [...]}``,
as SandboxBank's model-api does. Nothing else is sent: no ids, no labels.
"""

from __future__ import annotations

from typing import Any

import httpx

from rai.tevv.robustness import Scorer

TIMEOUT = 30.0


def http_scorer(url: str, client: httpx.Client | None = None) -> Scorer:
    """A scorer that posts each batch to ``url``. Any answer other than a 200 with
    the expected JSON is a ValueError: the step errors, it never passes."""
    session = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "rai-worker"})

    def score(rows: list[dict[str, float | str | None]]) -> tuple[list[float], dict[str, Any]]:
        try:
            response = session.post(url, json={"rows": rows})
        except httpx.HTTPError as error:
            raise ValueError(f"the scoring endpoint did not answer: {error}") from None
        if response.status_code != 200:
            raise ValueError(f"the scoring endpoint answered {response.status_code}: "
                             f"{response.text[:300]}")  # fmt: skip
        try:
            body = response.json()
            scores, model = body["scores"], body.get("model") or {}
        except (ValueError, KeyError, TypeError):
            raise ValueError("the scoring endpoint's answer is not {scores, model}") from None
        if not isinstance(scores, list) or not isinstance(model, dict):
            raise ValueError("the scoring endpoint's answer is not {scores, model}")
        return scores, model

    return score
