"""SandboxBank model-api (FR-09.3): scores named feature records with the fraud model.

RAI never runs a model: the model belongs to the system under test (decision
TEVV-METRIC). For robustness tests, RAI perturbs the inputs itself and sends them here,
to a target on the environment's allow-list. Run with ``sandboxbank model serve``
(the ``model`` extra).

* ``GET /readyz``: 200 once the model is trained (from its recipe, at startup), 503 before.
* ``POST /v1/score``: ``{"rows": [{feature: value or null, ...}, ...]}`` gives
  ``{"model": {...identity}, "scores": [...]}``, one score per row, in order. Every row
  names exactly the model's features; ``null`` is a missing value and a category
  outside the vocabulary counts as missing, as the model itself treats both.

No data is stored or logged: a request is scored and forgotten.
"""

from __future__ import annotations

import math
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from sandboxbank import __version__
from sandboxbank.model import CATEGORICAL, FEATURES, Model, Value, train

MAX_ROWS = 5_000
NAMES = [name for name, _ in FEATURES]


class ScoreIn(BaseModel):
    rows: list[dict[str, float | str | None]] = Field(min_length=1, max_length=MAX_ROWS)

    @field_validator("rows")
    @classmethod
    def _named(cls, rows: list[dict[str, Value]]) -> list[dict[str, Value]]:
        expected = set(NAMES)
        for n, row in enumerate(rows):
            if set(row) != expected:
                missing, extra = sorted(expected - set(row)), sorted(set(row) - expected)
                raise ValueError(f"row {n}: missing {missing}, unknown {extra}")
            for name, value in row.items():
                if name in CATEGORICAL and value is not None and not isinstance(value, str):
                    raise ValueError(f"row {n}: {name} is categorical, a string or null")
                if name not in CATEGORICAL and isinstance(value, str):
                    raise ValueError(f"row {n}: {name} is numeric, a number or null")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError(f"row {n}: {name} is not finite; send null for missing")
        return rows


def create_app(model: Model | None = None) -> FastAPI:
    """Build the app. Without ``model``, the recipe's model is trained at startup, in
    the background, so ``/readyz`` answers while it trains."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.model = model
        if model is None:

            def build() -> None:
                app.state.model = train()

            threading.Thread(target=build, daemon=True).start()
        yield

    app = FastAPI(title="SandboxBank model-api", version=__version__, lifespan=lifespan)

    def trained(request: Request) -> Model:
        found: Model | None = request.app.state.model
        if found is None:
            raise HTTPException(503, "the model is still training")
        return found

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> dict[str, Any]:
        return {"status": "ready", "model": trained(request).identity}

    @app.get("/v1/model")
    def describe(request: Request) -> dict[str, Any]:
        return {"model": trained(request).identity, "features": NAMES,
                "categorical": sorted(CATEGORICAL), "max_rows": MAX_ROWS}  # fmt: skip

    @app.post("/v1/score")
    def score(body: ScoreIn, request: Request) -> dict[str, Any]:
        found = trained(request)
        return {"model": found.identity, "scores": found.score_records(body.rows)}

    return app
