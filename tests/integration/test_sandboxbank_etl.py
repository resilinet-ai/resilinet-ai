"""P2 settlement ETL, ledger to warehouse, against a real PostgreSQL 16 (B-04).

The ledger tables and the warehouse schema share one test database here; in the
compose stack they are separate servers (ledger-db and analytics-db).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import date
from itertools import batched
from pathlib import Path
from typing import Any

import psycopg
import pytest

from sandboxbank.cli import main
from sandboxbank.dataset import write_dataset
from sandboxbank.etl import (
    load_chunk,
    migrate_warehouse,
    queue_settlement,
    run_cycle,
    run_service,
    settle_pending,
    watermark,
)
from sandboxbank.generator import GeneratorConfig
from sandboxbank.ledger import TransactionIn, migrate, post_transactions
from sandboxbank.loader import _read, load_reference, to_payload

DSN = os.environ.get("SANDBOXBANK_TEST_DSN", "")
if os.environ.get("CI") == "true" and not DSN:  # never skip silently in CI
    raise RuntimeError("SANDBOXBANK_TEST_DSN must be set in CI")
pytestmark = pytest.mark.skipif(not DSN, reason="SANDBOXBANK_TEST_DSN not set")

# Four 12-hour batches: two business dates, 2026-01-01 and 2026-01-02.
CONFIG = GeneratorConfig(
    seed=5, accounts=60, merchants=12, batches=4, rows_per_batch=400, batch_window_seconds=43_200
)
UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/none?connect_timeout=2"


def scalar(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()  # type: ignore[arg-type]
    return row[0] if row else None


def post_batch_file(conn: psycopg.Connection[Any], path: Path) -> None:
    for rows in batched(_read(path), 1_000):
        post_transactions(conn, [TransactionIn.model_validate(to_payload(r)) for r in rows])


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("sbx")
    write_dataset(CONFIG, folder)
    return folder


@pytest.fixture
def db(dataset: Path) -> Iterator[psycopg.Connection[Any]]:
    """Clean ledger with reference data and the first three batches posted; empty warehouse."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        migrate(conn)
        migrate_warehouse(conn)
        conn.execute(
            "TRUNCATE ledger_transactions, accounts, merchants RESTART IDENTITY CASCADE; "
            "TRUNCATE warehouse.dim_account, warehouse.dim_merchant, warehouse.fact_transaction, "
            "warehouse.settlement_account_daily, warehouse.settlement_merchant_daily, "
            "warehouse.etl_watermark, warehouse.settlement_pending, warehouse.etl_runs "
            "RESTART IDENTITY"
        )
        load_reference(conn, dataset)
        for name in ("B000000", "B000001", "B000002"):
            post_batch_file(conn, dataset / "transactions_raw" / f"{name}.csv")
        yield conn


def cycle(conn: psycopg.Connection[Any], **kwargs: Any) -> Any:
    return run_cycle(conn, conn, lag_seconds=0, **kwargs)


def assert_warehouse_matches_ledger(conn: psycopg.Connection[Any]) -> None:
    """Facts and both settlements equal aggregates computed independently from the ledger."""
    posted = "FROM ledger_transactions WHERE status = 'posted'"
    assert scalar(conn, "SELECT count(*) FROM warehouse.fact_transaction") == scalar(
        conn, f"SELECT count(*) {posted}"
    )
    assert (
        scalar(conn, "SELECT count(*) - count(DISTINCT txn_id) FROM warehouse.fact_transaction")
        == 0
    )
    assert scalar(conn, "SELECT sum(amount) FROM warehouse.fact_transaction") == scalar(
        conn, f"SELECT sum(amount) {posted}"
    )
    account_diff = f"""
        (SELECT business_date, account_id, debit_total, credit_total, txn_count
           FROM warehouse.settlement_account_daily)
        EXCEPT
        (SELECT (created_at AT TIME ZONE 'UTC')::date, account_id,
                COALESCE(sum(amount) FILTER (WHERE direction = 'debit'), 0),
                COALESCE(sum(amount) FILTER (WHERE direction = 'credit'), 0), count(*)::int
           {posted} GROUP BY 1, 2)
    """
    assert conn.execute(account_diff).fetchall() == []  # type: ignore[arg-type]
    merchant_diff = f"""
        (SELECT (created_at AT TIME ZONE 'UTC')::date, merchant_id, sum(amount), count(*)::int
           {posted} AND direction = 'debit' AND merchant_id IS NOT NULL GROUP BY 1, 2)
        EXCEPT
        (SELECT business_date, merchant_id, gross_amount, txn_count
           FROM warehouse.settlement_merchant_daily)
    """
    assert conn.execute(merchant_diff).fetchall() == []  # type: ignore[arg-type]
    assert scalar(conn, "SELECT count(*) FROM warehouse.settlement_pending") == 0


def test_first_cycle_loads_and_settles_everything(db: psycopg.Connection[Any]) -> None:
    summary = cycle(db, chunk_size=250)
    assert summary.dimensions_added == {"dim_account": 60, "dim_merchant": 12}
    assert summary.rows_read == 1_200
    assert summary.chunks == 5
    assert summary.rows_loaded == scalar(
        db, "SELECT count(*) FROM ledger_transactions WHERE status = 'posted'"
    )
    assert summary.dates_settled == [date(2026, 1, 1), date(2026, 1, 2)]
    assert watermark(db) == scalar(db, "SELECT max(seq) FROM ledger_transactions")
    assert_warehouse_matches_ledger(db)
    run = db.execute(
        "SELECT rows_read, rows_loaded, amount_loaded FROM warehouse.etl_runs"
    ).fetchall()
    assert run == [(1_200, summary.rows_loaded, summary.amount_loaded)]


def test_idle_cycle_changes_nothing(db: psycopg.Connection[Any]) -> None:
    cycle(db)
    again = cycle(db)
    assert (again.rows_read, again.chunks, again.dates_settled) == (0, 0, [])
    assert again.dimensions_added == {"dim_account": 0, "dim_merchant": 0}
    assert scalar(db, "SELECT count(*) FROM warehouse.etl_runs") == 1
    assert_warehouse_matches_ledger(db)


def test_new_ledger_rows_load_incrementally(db: psycopg.Connection[Any], dataset: Path) -> None:
    first = cycle(db)
    post_batch_file(db, dataset / "transactions_raw" / "B000003.csv")
    second = cycle(db)
    assert second.from_seq == first.to_seq
    assert second.rows_read == 400
    assert second.dates_settled == [date(2026, 1, 2)]
    assert_warehouse_matches_ledger(db)


def test_stop_mid_load_resumes_without_loss_or_duplicates(db: psycopg.Connection[Any]) -> None:
    # Protocol S12/S16: the job dies after committing one chunk of several.
    chunk = load_chunk(db, db, chunk_size=300, lag_seconds=0)
    assert chunk is not None
    assert watermark(db) == chunk.to_seq
    resumed = cycle(db, chunk_size=300)
    assert resumed.from_seq == chunk.to_seq
    assert resumed.rows_read == 900
    assert_warehouse_matches_ledger(db)


def test_crash_between_load_and_settlement_heals_next_cycle(db: psycopg.Connection[Any]) -> None:
    while load_chunk(db, db, chunk_size=500, lag_seconds=0):
        pass
    assert scalar(db, "SELECT count(*) FROM warehouse.settlement_pending") == 2
    assert scalar(db, "SELECT count(*) FROM warehouse.settlement_account_daily") == 0
    healed = cycle(db)
    assert healed.rows_read == 0
    assert healed.dates_settled == [date(2026, 1, 1), date(2026, 1, 2)]
    assert_warehouse_matches_ledger(db)


def test_a_failed_chunk_commits_nothing(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_execute = db.execute

    def failing_execute(query: Any, *args: Any, **kwargs: Any) -> Any:
        if "UPDATE warehouse.etl_watermark" in str(query):
            raise psycopg.OperationalError("connection dropped (simulated)")
        return real_execute(query, *args, **kwargs)

    monkeypatch.setattr(db, "execute", failing_execute)
    with pytest.raises(psycopg.OperationalError):
        load_chunk(db, db, chunk_size=300, lag_seconds=0)
    monkeypatch.undo()
    assert scalar(db, "SELECT count(*) FROM warehouse.fact_transaction") == 0
    assert scalar(db, "SELECT count(*) FROM warehouse.settlement_pending") == 0
    assert watermark(db) == 0


def test_rows_inside_the_commit_lag_wait_for_the_next_cycle(db: psycopg.Connection[Any]) -> None:
    waiting = run_cycle(db, db, lag_seconds=3_600)
    assert waiting.rows_read == 0
    assert watermark(db) == 0


def test_duplicate_dimension_key_inflates_settlement(db: psycopg.Connection[Any]) -> None:
    # D12 must be possible in the system under test, or RAI cannot be scored on it.
    cycle(db)
    merchant, day, before = db.execute(
        "SELECT merchant_id, business_date, txn_count FROM warehouse.settlement_merchant_daily "
        "ORDER BY txn_count DESC LIMIT 1"
    ).fetchone() or (None, None, None)
    db.execute(
        "INSERT INTO warehouse.dim_merchant (merchant_id, name, mcc, category, country) "
        "SELECT merchant_id, name, mcc, category, country FROM warehouse.dim_merchant "
        "WHERE merchant_id = %s",
        (merchant,),
    )
    queue_settlement(db, [day])
    assert settle_pending(db) == [day]
    after = scalar(
        db,
        "SELECT txn_count FROM warehouse.settlement_merchant_daily "
        "WHERE merchant_id = %s AND business_date = %s",
        (merchant, day),
    )
    assert after == 2 * before


# ---- service mode and CLI ------------------------------------------------------


def test_service_once(db: psycopg.Connection[Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_service(DSN, DSN, once=True, lag_seconds=0) == 0
    out = capsys.readouterr().out
    assert '"event": "connected"' in out
    assert '"event": "cycle"' in out
    assert_warehouse_matches_ledger(db)


def test_service_loop_stops_on_signal(
    db: psycopg.Connection[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    stop = threading.Event()
    stop.set()  # already asked to stop: one cycle, then exit
    assert run_service(DSN, DSN, interval=0.01, lag_seconds=0, stop=stop) == 0
    assert '"event": "stopped"' in capsys.readouterr().out


def test_service_survives_an_unreachable_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_service(UNREACHABLE, UNREACHABLE, once=True) == 1
    stop = threading.Event()
    stop.set()
    assert run_service(UNREACHABLE, UNREACHABLE, interval=0.01, stop=stop) == 0
    out = capsys.readouterr().out
    assert out.count('"event": "database_unavailable"') == 2
    assert '"event": "stopped"' in out


def test_cli_etl(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SANDBOXBANK_ANALYTICS_DSN", raising=False)
    monkeypatch.setenv("SANDBOXBANK_LEDGER_DSN", DSN)
    assert main(["etl", "--once"]) == 2
    monkeypatch.setenv("SANDBOXBANK_ANALYTICS_DSN", DSN)
    assert main(["etl", "--once", "--chunk", "0"]) == 2
    assert main(["etl", "--once", "--lag", "0"]) == 0
    assert '"event": "cycle"' in capsys.readouterr().out
    assert_warehouse_matches_ledger(db)
