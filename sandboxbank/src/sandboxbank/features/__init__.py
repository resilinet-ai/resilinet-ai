"""SandboxBank feature-job: pipeline P3, the warehouse to the fraud feature table.

P3 is the downstream machine-learning pipeline: it turns posted transactions in the
analytics warehouse (P2's output) into the fraud model's inputs, has each one scored by
the model-api, and stores both in ``features.fraud_features``. It exists so the study
has a third pipeline, with its own dependencies to fail (Protocol S10, S13, S18): the
warehouse database, and a model service reached over HTTP.

Each cycle reads ``warehouse.fact_transaction`` after the pipeline's watermark, in
``ledger_seq`` order and in chunks. A chunk's rows are scored first; then its features,
scores and the watermark's move commit in one transaction. So a stop, a restart or a
scoring outage resumes at the last committed chunk, with nothing lost or doubled, and a
chunk is never stored without its scores.

Features are computed from what the warehouse holds, as a real downstream pipeline
would: the account's type and transaction limit and the merchant's category from the
dimensions. The warehouse keeps posted transactions only, so ``status`` is ``posted``.

In service mode, an unavailable database or model-api is logged and retried after the
interval, so the job recovers by itself once the fault clears.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import psycopg

from sandboxbank.health import Readiness
from sandboxbank.model import FEATURES, Value

PIPELINE = "p3_fraud_features"
DEFAULT_CHUNK = 5_000  # the model-api's limit per request
SCORE_TIMEOUT = 30.0
NAMES = [name for name, _ in FEATURES]
COLUMNS = ("txn_id", "ledger_seq", "business_date", *NAMES, "is_fraud", "score",
           "model_recipe_sha256")  # fmt: skip


class ScoringUnavailable(Exception):
    """The model-api did not answer, or answered something other than scores."""


def features_sql() -> str:
    return resources.files(__package__).joinpath("features.sql").read_text(encoding="utf-8")


def migrate_features(wh: psycopg.Connection[Any]) -> None:
    with wh.transaction():
        wh.execute(features_sql())


def log(event: str, **fields: Any) -> None:
    """One JSON line per event on stdout (NFR-OPS-1)."""
    record = {"ts": datetime.now(UTC).isoformat(), "service": "feature-job", "event": event}
    print(json.dumps(record | fields, default=str), flush=True)


def watermark(wh: psycopg.Connection[Any]) -> int:
    row = wh.execute(
        "SELECT ledger_seq FROM features.feature_watermark WHERE pipeline = %s", (PIPELINE,)
    ).fetchone()
    return int(row[0]) if row else 0


_SELECT = """
SELECT f.txn_id, f.ledger_seq, f.business_date, f.amount::float8, f.channel, f.direction,
       f.merchant_id, f.is_fraud, a.account_type, a.txn_limit::float8, m.category
  FROM warehouse.fact_transaction f
  LEFT JOIN LATERAL (SELECT account_type, txn_limit FROM warehouse.dim_account d
                      WHERE d.account_id = f.account_id ORDER BY d.account_sk LIMIT 1) a ON true
  LEFT JOIN LATERAL (SELECT category FROM warehouse.dim_merchant d
                      WHERE d.merchant_id = f.merchant_id ORDER BY d.merchant_sk LIMIT 1) m ON true
 WHERE f.ledger_seq > %s
 ORDER BY f.ledger_seq
 LIMIT %s
"""


def record(row: tuple[Any, ...]) -> dict[str, Value]:
    """A warehouse row as the model's named inputs (see ``sandboxbank.model``)."""
    (_txn, _seq, _date, amount, channel, direction, merchant_id, _fraud,
     account_type, limit, category) = row  # fmt: skip
    return named(amount, channel, direction, merchant_id, account_type, limit, category)


def named(
    amount: float, channel: str | None, direction: str, merchant_id: str | None,
    account_type: str | None, limit: float | None, category: str | None,
) -> dict[str, Value]:  # fmt: skip
    """The model's named inputs from a posted transaction and its dimensions. The P3
    fixtures (``sandboxbank.fixtures``) use the same function, so they cannot drift."""
    return {
        "amount": float(amount),
        "amount_to_limit": float(amount) / limit if limit else None,
        "channel": channel or None,
        "credit": 1.0 if direction == "credit" else 0.0,
        "status": "posted",  # the warehouse holds posted transactions only
        "account_type": account_type,
        "has_merchant": 1.0 if merchant_id else 0.0,
        "merchant_category": category if merchant_id else None,
    }


@dataclass(frozen=True)
class Scored:
    scores: list[float]
    recipe_sha256: str


def score(url: str, records: list[dict[str, Value]], timeout: float = SCORE_TIMEOUT) -> Scored:
    """Scores from the model-api, in order. Anything else is ScoringUnavailable."""
    body = json.dumps({"rows": records}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - the configured model-api URL
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "sandboxbank-feature-job"},
    )  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            answer = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise ScoringUnavailable(str(error)) from None
    scores, model = answer.get("scores"), answer.get("model") or {}
    if not isinstance(scores, list) or len(scores) != len(records):
        raise ScoringUnavailable("the model-api did not return one score per row")
    recipe = model.get("recipe_sha256")
    if not isinstance(recipe, str) or len(recipe) != 64:
        raise ScoringUnavailable("the model-api did not name its model")
    return Scored([float(s) for s in scores], recipe)


@dataclass
class Chunk:
    from_seq: int
    to_seq: int
    rows: int
    recipe_sha256: str


def load_chunk(
    wh: psycopg.Connection[Any], score_url: str, *, chunk_size: int = DEFAULT_CHUNK
) -> Chunk | None:
    """Score and store the next chunk after the watermark; None when there is none."""
    start = watermark(wh)
    rows = wh.execute(_SELECT, (start, chunk_size)).fetchall()
    if not rows:
        return None
    records = [record(row) for row in rows]
    scored = score(score_url, records)  # before anything is written
    with wh.transaction():
        wh.execute(
            "INSERT INTO features.feature_watermark (pipeline, ledger_seq) VALUES (%s, 0) "
            "ON CONFLICT (pipeline) DO NOTHING",
            (PIPELINE,),
        )
        locked = wh.execute(
            "SELECT ledger_seq FROM features.feature_watermark WHERE pipeline = %s FOR UPDATE",
            (PIPELINE,),
        ).fetchone()
        if locked is None or int(locked[0]) != start:  # another job moved it: start over
            return Chunk(start, start, 0, scored.recipe_sha256)
        with wh.cursor().copy(
            f"COPY features.fraud_features ({', '.join(COLUMNS)}) FROM STDIN"
        ) as copy:
            for row, named, value in zip(rows, records, scored.scores, strict=True):
                copy.write_row((row[0], row[1], row[2], *(named[n] for n in NAMES), row[7],
                                value, scored.recipe_sha256))  # fmt: skip
        last = int(rows[-1][1])
        wh.execute(
            "UPDATE features.feature_watermark SET ledger_seq = %s, "
            "updated_at = clock_timestamp() WHERE pipeline = %s",
            (last, PIPELINE),
        )
    return Chunk(start, last, len(rows), scored.recipe_sha256)


@dataclass
class CycleSummary:
    chunks: int = 0
    from_seq: int = 0
    to_seq: int = 0
    rows_scored: int = 0
    recipe_sha256: str | None = None


def run_cycle(
    wh: psycopg.Connection[Any], score_url: str, *, chunk_size: int = DEFAULT_CHUNK
) -> CycleSummary:
    """Every available chunk; records a feature_runs row when anything was scored."""
    started = datetime.now(UTC)
    summary = CycleSummary()
    summary.from_seq = summary.to_seq = watermark(wh)
    while (chunk := load_chunk(wh, score_url, chunk_size=chunk_size)) is not None:
        summary.chunks += 1
        summary.to_seq = chunk.to_seq
        summary.rows_scored += chunk.rows
        summary.recipe_sha256 = chunk.recipe_sha256
    if summary.rows_scored:
        with wh.transaction():
            wh.execute(
                "INSERT INTO features.feature_runs (started_at, from_seq, to_seq, rows_read, "
                "rows_scored, model_recipe_sha256) VALUES (%s, %s, %s, %s, %s, %s)",
                (started, summary.from_seq, summary.to_seq, summary.rows_scored,
                 summary.rows_scored, summary.recipe_sha256),
            )  # fmt: skip
    return summary


def run_service(
    analytics_dsn: str,
    score_url: str,
    *,
    once: bool = False,
    interval: float = 60.0,
    chunk_size: int = DEFAULT_CHUNK,
    stop: threading.Event | None = None,
    readiness: Readiness | None = None,
) -> int:
    """Run cycles until stopped. With ``once``, run one cycle and return its exit code.
    ``readiness`` (the freshness probe, ``sandboxbank.health``) learns each outcome."""
    stop = stop or threading.Event()
    while True:
        try:
            with psycopg.connect(analytics_dsn, autocommit=True) as wh:
                migrate_features(wh)
                log("connected")
                while True:
                    summary = run_cycle(wh, score_url, chunk_size=chunk_size)
                    log("cycle", **asdict(summary))
                    if readiness is not None:  # run_cycle drains every chunk: caught up
                        readiness.cycle_succeeded()
                    if once:
                        return 0
                    if stop.wait(interval):
                        log("stopped")
                        return 0
        except psycopg.OperationalError as error:
            log("database_unavailable", error=str(error).splitlines()[0] if str(error) else "")
            if readiness is not None:
                readiness.cycle_failed("analytics-db unavailable")
        except ScoringUnavailable as error:
            log("scoring_unavailable", error=str(error)[:300])
            if readiness is not None:
                readiness.cycle_failed("model-api unavailable")
        if once:
            return 1
        if stop.wait(interval):
            log("stopped")
            return 0
