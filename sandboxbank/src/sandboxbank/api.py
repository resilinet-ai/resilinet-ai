"""SandboxBank payments-api (B-03): posts transactions to ledger-db idempotently.

Run with ``sandboxbank serve`` or ``uvicorn sandboxbank.api:app``. The ledger
connection string comes from ``SANDBOXBANK_LEDGER_DSN``; it is never logged.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from sandboxbank import __version__
from sandboxbank.ledger import (
    MAX_BATCH,
    TransactionIn,
    get_transaction,
    ledger_head,
    migrate,
    post_transactions,
)

DSN_ENV = "SANDBOXBANK_LEDGER_DSN"

STATUS_FOR_OUTCOME = {"created": 201, "replayed": 200, "conflict": 409, "rejected": 422}


class BatchIn(BaseModel):
    transactions: list[TransactionIn] = Field(min_length=1, max_length=MAX_BATCH)


def _pool(request: Request) -> ConnectionPool:
    return request.app.state.pool  # type: ignore[no-any-return]


Pool = Annotated[ConnectionPool, Depends(_pool)]


def create_app(dsn: str | None = None) -> FastAPI:
    """Build the app; the pool opens and the schema is migrated at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conninfo = dsn or os.environ.get(DSN_ENV)
        if not conninfo:
            raise RuntimeError(f"set {DSN_ENV} to the ledger-db connection string")
        with ConnectionPool(conninfo, min_size=1, max_size=8, open=True) as pool:
            with pool.connection() as conn:
                migrate(conn)
            app.state.pool = pool
            yield

    app = FastAPI(
        title="SandboxBank payments-api",
        version=__version__,
        description="Synthetic payments system under test for RAI. Synthetic data only.",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(db: Pool) -> dict[str, str]:
        with db.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ready"}

    @app.post("/v1/transactions", status_code=201)
    def post_one(txn: TransactionIn, response: Response, db: Pool) -> dict[str, Any]:
        with db.connection() as conn:
            (result,) = post_transactions(conn, [txn])
        response.status_code = STATUS_FOR_OUTCOME[result.outcome]
        return asdict(result)

    @app.post("/v1/transactions/batch")
    def post_batch(batch: BatchIn, db: Pool) -> dict[str, Any]:
        with db.connection() as conn:
            results = post_transactions(conn, batch.transactions)
        counts = dict.fromkeys(STATUS_FOR_OUTCOME, 0)
        for result in results:
            counts[result.outcome] += 1
        return {"counts": counts, "results": [asdict(result) for result in results]}

    @app.get("/v1/transactions/{txn_id}")
    def get_one(txn_id: UUID, db: Pool) -> dict[str, Any]:
        with db.connection() as conn:
            row = get_transaction(conn, txn_id)
        if row is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        return row

    @app.get("/v1/ledger/head")
    def head(db: Pool) -> dict[str, Any]:
        with db.connection() as conn:
            return asdict(ledger_head(conn))

    return app


app = create_app()
