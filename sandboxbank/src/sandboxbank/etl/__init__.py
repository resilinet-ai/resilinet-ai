"""SandboxBank etl-job (B-04): pipeline P2, ledger-db to the analytics warehouse.

Each cycle (decision log: SBX-ETL):

1. **Dimensions:** copy accounts and merchants that the warehouse has not seen yet.
2. **Facts:** read ledger rows after the watermark in ``seq`` order, in chunks. Each
   chunk's posted rows, its watermark move and the business dates it touched
   commit in one warehouse transaction, so a stop or restart mid-load (Protocol
   S12, S16) resumes at the last committed chunk with nothing lost or doubled.
3. **Settlement:** rebuild daily account and merchant settlement for every queued
   business date, then dequeue those dates in the same transaction.

Ledger ``seq`` values are assigned at insert, not commit, so a row with a lower
``seq`` can become visible after a higher one. The ETL therefore only reads rows
committed at least ``lag_seconds`` ago; ledger posting transactions are far
shorter than that.

In service mode, connection errors (Protocol S04, S07, S09) are logged and
retried after the interval, so the job recovers by itself once the fault clears.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import resources
from typing import Any

import psycopg

PIPELINE = "p2_settlement"
DEFAULT_CHUNK = 10_000
DEFAULT_LAG_SECONDS = 5.0

FACT_COLUMNS = (
    "business_date",
    "ledger_seq",
    "txn_id",
    "account_id",
    "merchant_id",
    "channel",
    "direction",
    "amount",
    "currency",
    "created_at",
    "is_fraud",
    "committed_at",
)


def warehouse_sql() -> str:
    return resources.files(__package__).joinpath("warehouse.sql").read_text(encoding="utf-8")


def migrate_warehouse(wh: psycopg.Connection[Any]) -> None:
    """Create or update the warehouse schema; idempotent."""
    with wh.transaction():
        wh.execute(warehouse_sql())  # trusted packaged SQL


def log(event: str, **fields: Any) -> None:
    """One JSON line per event on stdout (NFR-OPS-1)."""
    record = {"ts": datetime.now(UTC).isoformat(), "service": "etl-job", "event": event}
    print(json.dumps(record | fields, default=str), flush=True)


# ---- step 1: dimensions -------------------------------------------------------

_DIMENSIONS = (
    (
        "dim_account",
        "accounts",
        (
            "account_id",
            "customer_id",
            "account_type",
            "currency",
            "status",
            "opened_on",
            "txn_limit",
        ),
    ),
    ("dim_merchant", "merchants", ("merchant_id", "name", "mcc", "category", "country")),
)


def refresh_dimensions(
    ledger: psycopg.Connection[Any], wh: psycopg.Connection[Any]
) -> dict[str, int]:
    """Append reference rows whose natural key the warehouse does not have yet."""
    added: dict[str, int] = {}
    with wh.transaction():
        for target, source, columns in _DIMENSIONS:
            key = columns[0]
            known = {row[0] for row in wh.execute(f"SELECT DISTINCT {key} FROM warehouse.{target}")}  # noqa: S608
            fresh = [
                row
                for row in ledger.execute(
                    f"SELECT {', '.join(columns)} FROM {source} ORDER BY {key}"  # noqa: S608
                )
                if row[0] not in known
            ]
            if fresh:
                with wh.cursor().copy(
                    f"COPY warehouse.{target} ({', '.join(columns)}) FROM STDIN"
                ) as copy:
                    for row in fresh:
                        copy.write_row(row)
            added[target] = len(fresh)
    return added


# ---- step 2: facts --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chunk:
    from_seq: int
    to_seq: int
    rows_read: int
    rows_loaded: int
    amount_loaded: Decimal


def watermark(wh: psycopg.Connection[Any]) -> int:
    row = wh.execute(
        "SELECT ledger_seq FROM warehouse.etl_watermark WHERE pipeline = %s", (PIPELINE,)
    ).fetchone()
    return int(row[0]) if row else 0


def load_chunk(
    ledger: psycopg.Connection[Any],
    wh: psycopg.Connection[Any],
    *,
    chunk_size: int = DEFAULT_CHUNK,
    lag_seconds: float = DEFAULT_LAG_SECONDS,
) -> Chunk | None:
    """Load the next chunk after the watermark; ``None`` when there is nothing new."""
    with wh.transaction():
        wh.execute(
            "INSERT INTO warehouse.etl_watermark (pipeline, ledger_seq) VALUES (%s, 0) "
            "ON CONFLICT (pipeline) DO NOTHING",
            (PIPELINE,),
        )
        # Row lock: a second etl-job instance waits instead of loading the same chunk.
        locked = wh.execute(
            "SELECT ledger_seq FROM warehouse.etl_watermark WHERE pipeline = %s FOR UPDATE",
            (PIPELINE,),
        ).fetchone()
        start = int(locked[0]) if locked else 0
        rows = ledger.execute(
            "SELECT seq, txn_id, account_id, merchant_id, channel, direction, amount, currency, "
            "created_at, is_fraud, committed_at, status FROM ledger_transactions "
            "WHERE seq > %s AND committed_at < clock_timestamp() - make_interval(secs => %s) "
            "ORDER BY seq LIMIT %s",
            (start, lag_seconds, chunk_size),
        ).fetchall()
        if not rows:
            return None

        posted = [row for row in rows if row[11] == "posted"]
        dates = sorted({row[8].astimezone(UTC).date() for row in posted})
        if posted:
            with wh.cursor().copy(
                f"COPY warehouse.fact_transaction ({', '.join(FACT_COLUMNS)}) FROM STDIN"
            ) as copy:
                for row in posted:
                    copy.write_row((row[8].astimezone(UTC).date(), *row[:11]))
            wh.cursor().executemany(
                "INSERT INTO warehouse.settlement_pending (business_date) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                [(day,) for day in dates],
            )
        end = int(rows[-1][0])
        wh.execute(
            "UPDATE warehouse.etl_watermark SET ledger_seq = %s, updated_at = clock_timestamp() "
            "WHERE pipeline = %s",
            (end, PIPELINE),
        )
    return Chunk(start, end, len(rows), len(posted), sum((row[6] for row in posted), Decimal(0)))


# ---- step 3: settlement ---------------------------------------------------------

_SETTLE_ACCOUNTS = """
INSERT INTO warehouse.settlement_account_daily (
    business_date, account_id, account_type, currency,
    debit_total, credit_total, net_amount, txn_count
)
SELECT f.business_date, f.account_id, a.account_type, f.currency,
       COALESCE(sum(f.amount) FILTER (WHERE f.direction = 'debit'), 0),
       COALESCE(sum(f.amount) FILTER (WHERE f.direction = 'credit'), 0),
       COALESCE(sum(f.amount) FILTER (WHERE f.direction = 'credit'), 0)
         - COALESCE(sum(f.amount) FILTER (WHERE f.direction = 'debit'), 0),
       count(*)
FROM warehouse.fact_transaction f
JOIN warehouse.dim_account a USING (account_id)
WHERE f.business_date = ANY(%s)
GROUP BY f.business_date, f.account_id, a.account_type, f.currency
"""

_SETTLE_MERCHANTS = """
INSERT INTO warehouse.settlement_merchant_daily (
    business_date, merchant_id, category, gross_amount, txn_count
)
SELECT f.business_date, f.merchant_id, m.category, sum(f.amount), count(*)
FROM warehouse.fact_transaction f
JOIN warehouse.dim_merchant m USING (merchant_id)
WHERE f.business_date = ANY(%s) AND f.direction = 'debit'
GROUP BY f.business_date, f.merchant_id, m.category
"""


def settle_pending(wh: psycopg.Connection[Any]) -> list[date]:
    """Rebuild settlement for every queued business date; return the dates settled."""
    with wh.transaction():
        dates = [
            row[0]
            for row in wh.execute(
                "SELECT business_date FROM warehouse.settlement_pending "
                "ORDER BY business_date FOR UPDATE"
            )
        ]
        if not dates:
            return []
        for table in ("settlement_account_daily", "settlement_merchant_daily"):
            wh.execute(f"DELETE FROM warehouse.{table} WHERE business_date = ANY(%s)", (dates,))  # noqa: S608
        wh.execute(_SETTLE_ACCOUNTS, (dates,))
        wh.execute(_SETTLE_MERCHANTS, (dates,))
        wh.execute(
            "DELETE FROM warehouse.settlement_pending WHERE business_date = ANY(%s)", (dates,)
        )
    return dates


def queue_settlement(wh: psycopg.Connection[Any], dates: list[date]) -> None:
    """Queue dates for rebuilding, for example after a fixture or a backfill."""
    with wh.transaction():
        wh.cursor().executemany(
            "INSERT INTO warehouse.settlement_pending (business_date) VALUES (%s) "
            "ON CONFLICT DO NOTHING",
            [(day,) for day in dates],
        )


# ---- one cycle and the service loop -----------------------------------------------


@dataclass(slots=True)
class CycleSummary:
    dimensions_added: dict[str, int] = field(default_factory=dict)
    chunks: int = 0
    from_seq: int = 0
    to_seq: int = 0
    rows_read: int = 0
    rows_loaded: int = 0
    amount_loaded: Decimal = Decimal(0)
    dates_settled: list[date] = field(default_factory=list)


def run_cycle(
    ledger: psycopg.Connection[Any],
    wh: psycopg.Connection[Any],
    *,
    chunk_size: int = DEFAULT_CHUNK,
    lag_seconds: float = DEFAULT_LAG_SECONDS,
) -> CycleSummary:
    """Dimensions, then every available chunk, then settlement; records an etl_runs row."""
    started = datetime.now(UTC)
    summary = CycleSummary(dimensions_added=refresh_dimensions(ledger, wh))
    summary.from_seq = summary.to_seq = watermark(wh)
    while (
        chunk := load_chunk(ledger, wh, chunk_size=chunk_size, lag_seconds=lag_seconds)
    ) is not None:
        summary.chunks += 1
        summary.to_seq = chunk.to_seq
        summary.rows_read += chunk.rows_read
        summary.rows_loaded += chunk.rows_loaded
        summary.amount_loaded += chunk.amount_loaded
    summary.dates_settled = settle_pending(wh)
    if summary.rows_read or summary.dates_settled:
        with wh.transaction():
            wh.execute(
                "INSERT INTO warehouse.etl_runs (started_at, from_seq, to_seq, rows_read, "
                "rows_loaded, amount_loaded, dates_settled) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    started,
                    summary.from_seq,
                    summary.to_seq,
                    summary.rows_read,
                    summary.rows_loaded,
                    summary.amount_loaded,
                    len(summary.dates_settled),
                ),
            )
    return summary


def run_service(
    ledger_dsn: str,
    analytics_dsn: str,
    *,
    once: bool = False,
    interval: float = 60.0,
    chunk_size: int = DEFAULT_CHUNK,
    lag_seconds: float = DEFAULT_LAG_SECONDS,
    stop: threading.Event | None = None,
) -> int:
    """Run cycles until stopped. With ``once``, run one cycle and return its exit code."""
    stop = stop or threading.Event()
    while True:
        try:
            with (
                psycopg.connect(ledger_dsn, autocommit=True) as ledger,
                psycopg.connect(analytics_dsn, autocommit=True) as wh,
            ):
                migrate_warehouse(wh)
                log("connected")
                while True:
                    summary = run_cycle(ledger, wh, chunk_size=chunk_size, lag_seconds=lag_seconds)
                    log("cycle", **asdict(summary))
                    if once:
                        return 0
                    if stop.wait(interval):
                        log("stopped")
                        return 0
        except psycopg.OperationalError as error:
            log("database_unavailable", error=str(error).splitlines()[0] if str(error) else "")
            if once:
                return 1
            if stop.wait(interval):
                log("stopped")
                return 0


def main_loop_signals(stop: threading.Event) -> None:  # pragma: no cover - process wiring
    """Stop cleanly on SIGTERM (docker stop) and Ctrl+C."""
    import signal

    def handle(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, handle)
