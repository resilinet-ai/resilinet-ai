"""P3 fraud feature pipeline, warehouse to feature table, against a real PostgreSQL 16.

The ledger, the warehouse and the feature schema share one test database here; in the
compose stack the feature table lives in analytics-db with the warehouse. A stub
model-api, served from a thread, scores rows the way the real one does: in order, and
naming its model."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest

from sandboxbank.cli import main
from sandboxbank.etl import migrate_warehouse, run_cycle
from sandboxbank.features import NAMES, load_chunk, migrate_features, record, run_service
from sandboxbank.features import run_cycle as feature_cycle
from sandboxbank.features import watermark as feature_watermark
from sandboxbank.ledger import migrate
from sandboxbank.loader import load_reference
from tests.integration.test_sandboxbank_etl import DSN, dataset, post_batch_file, scalar  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="SANDBOXBANK_TEST_DSN not set")

RECIPE = "a" * 64


class StubModel:
    """Scores large amounts high; ``down`` makes it answer 503, like a model still training."""

    def __init__(self) -> None:
        self.down = False
        self.rows_seen: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                rows = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["rows"]
                if stub.down:
                    self.send_response(503)
                    self.end_headers()
                    return
                stub.rows_seen.extend(rows)
                scores = [min(1.0, r["amount"] / 5000) for r in rows]
                body = json.dumps({"model": {"recipe_sha256": RECIPE}, "scores": scores})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *_: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/score"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def model() -> Iterator[StubModel]:
    stub = StubModel()
    yield stub
    stub.server.shutdown()


@pytest.fixture
def wh(dataset: Path) -> Iterator[psycopg.Connection[Any]]:  # noqa: F811
    """Three batches posted to the ledger and loaded into the warehouse; no features yet."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        migrate(conn)
        migrate_warehouse(conn)
        migrate_features(conn)
        conn.execute(
            "TRUNCATE ledger_transactions, accounts, merchants RESTART IDENTITY CASCADE; "
            "TRUNCATE warehouse.dim_account, warehouse.dim_merchant, warehouse.fact_transaction, "
            "warehouse.settlement_account_daily, warehouse.settlement_merchant_daily, "
            "warehouse.etl_watermark, warehouse.settlement_pending, warehouse.etl_runs, "
            "features.fraud_features, features.feature_watermark, features.feature_runs "
            "RESTART IDENTITY"
        )
        load_reference(conn, dataset)
        for name in ("B000000", "B000001", "B000002"):
            post_batch_file(conn, dataset / "transactions_raw" / f"{name}.csv")
        run_cycle(conn, conn, lag_seconds=0)
        yield conn


def test_every_posted_transaction_gets_its_features_and_score_once(
    wh: psycopg.Connection[Any], model: StubModel
) -> None:
    facts = scalar(wh, "SELECT count(*) FROM warehouse.fact_transaction")
    assert facts > 0
    summary = feature_cycle(wh, model.url, chunk_size=250)
    assert summary.rows_scored == facts
    assert summary.chunks == -(-facts // 250)
    assert summary.to_seq == scalar(wh, "SELECT max(ledger_seq) FROM warehouse.fact_transaction")
    assert feature_watermark(wh) == summary.to_seq
    assert scalar(wh, "SELECT count(*) FROM features.fraud_features") == facts
    assert (
        scalar(wh, "SELECT count(DISTINCT model_recipe_sha256) FROM features.fraud_features") == 1
    )
    # The rows sent are exactly the model's inputs, nothing else: no id, no label.
    assert set(model.rows_seen[0]) == set(NAMES)
    assert {r["status"] for r in model.rows_seen} == {"posted"}

    # Features agree with the warehouse they came from.
    mismatched = scalar(
        wh,
        """
        SELECT count(*) FROM features.fraud_features x
          JOIN warehouse.fact_transaction f USING (txn_id)
         WHERE x.amount <> f.amount::float8 OR x.is_fraud <> f.is_fraud
            OR x.credit <> (f.direction = 'credit')::int OR x.ledger_seq <> f.ledger_seq""",
    )
    assert mismatched == 0
    assert feature_cycle(wh, model.url).rows_scored == 0  # nothing new: nothing doubled
    assert scalar(wh, "SELECT count(*) FROM features.feature_runs") == 1


def test_a_scoring_outage_stores_nothing_and_the_next_cycle_resumes(
    wh: psycopg.Connection[Any], model: StubModel, capsys: pytest.CaptureFixture[str]
) -> None:
    assert load_chunk(wh, model.url, chunk_size=100) is not None  # one chunk committed
    stored, mark = scalar(wh, "SELECT count(*) FROM features.fraud_features"), feature_watermark(wh)
    model.down = True
    dsn = wh.info.dsn + " password=" + (wh.info.password or "")
    assert run_service(dsn, model.url, once=True, chunk_size=100) == 1  # S13: model-api away
    assert "scoring_unavailable" in capsys.readouterr().out
    assert (scalar(wh, "SELECT count(*) FROM features.fraud_features"), feature_watermark(wh)) == (
        stored, mark)  # fmt: skip
    model.down = False
    assert run_service(dsn, model.url, once=True, chunk_size=100) == 0
    facts = scalar(wh, "SELECT count(*) FROM warehouse.fact_transaction")
    assert scalar(wh, "SELECT count(*) FROM features.fraud_features") == facts
    assert scalar(wh, "SELECT count(*) - count(DISTINCT txn_id) FROM features.fraud_features") == 0


def test_an_unreachable_model_api_is_retried_not_fatal(wh: psycopg.Connection[Any]) -> None:
    dsn = wh.info.dsn + " password=" + (wh.info.password or "")
    assert run_service(dsn, "http://127.0.0.1:1/v1/score", once=True) == 1
    assert feature_watermark(wh) == 0


def test_features_follow_the_warehouse_dimensions() -> None:
    row = ("t", 7, "2026-01-01", 250.0, "ecommerce", "debit", "M-1", True, "checking", 1000.0,
           "electronics")  # fmt: skip
    named = record(row)
    assert named == {"amount": 250.0, "amount_to_limit": 0.25, "channel": "ecommerce",
                     "credit": 0.0, "status": "posted", "account_type": "checking",
                     "has_merchant": 1.0, "merchant_category": "electronics"}  # fmt: skip
    unknown = record(("t", 8, "2026-01-01", 10.0, "ach", "credit", None, False, None, None, None))
    assert (unknown["amount_to_limit"], unknown["account_type"]) == (None, None)
    assert (unknown["has_merchant"], unknown["merchant_category"], unknown["credit"]) == (
        0.0, None, 1.0)  # fmt: skip


def test_the_cli_needs_the_analytics_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOXBANK_ANALYTICS_DSN", raising=False)
    assert main(["features", "--once"]) == 2
    monkeypatch.setenv("SANDBOXBANK_ANALYTICS_DSN", DSN)
    assert main(["features", "--once", "--chunk", "6000"]) == 2
