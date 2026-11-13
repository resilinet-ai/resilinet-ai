"""Load a generated dataset into SandboxBank (B-03).

* Reference tables (accounts, merchants) go straight into ledger-db; re-running
  is harmless because existing keys are skipped.
* Transaction batches go through the payments-api, the same path a real
  upstream feed takes, so chaos faults on that path affect them.
"""

from __future__ import annotations

import csv
import json
import urllib.request
from collections import Counter
from collections.abc import Iterator
from itertools import batched
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from sandboxbank.ledger import MAX_BATCH, migrate
from sandboxbank.schema import column_names


def _read(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def load_reference(conn: psycopg.Connection[Any], data_dir: Path) -> dict[str, int]:
    """Insert accounts and merchants; return how many new rows each table got."""
    migrate(conn)
    inserted: dict[str, int] = {}
    for table, key in (("accounts", "account_id"), ("merchants", "merchant_id")):
        columns = column_names(table)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608 - fixed identifiers
            f"VALUES ({', '.join(['%s'] * len(columns))}) ON CONFLICT ({key}) DO NOTHING"
        )
        with conn.transaction(), conn.cursor() as cursor:
            before = cursor.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            cursor.executemany(
                sql,
                [tuple(row[c] for c in columns) for row in _read(data_dir / f"{table}.csv")],
            )
            after = cursor.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        inserted[table] = (after[0] if after else 0) - (before[0] if before else 0)
    return inserted


def to_payload(row: dict[str, str]) -> dict[str, Any]:
    """CSV row of transactions_raw to the payments-api JSON shape."""
    payload: dict[str, Any] = dict(row)
    payload["merchant_id"] = row["merchant_id"] or None
    payload["is_fraud"] = row["is_fraud"] == "true"
    return payload


def _post_json(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - scheme checked in post_batches
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        result: dict[str, Any] = json.load(response)
    return result


def post_batches(
    api_url: str,
    batch_files: list[Path],
    *,
    chunk: int = MAX_BATCH,
    timeout: float = 30.0,
) -> Counter[str]:
    """Post every row of the given batch files; return outcome counts."""
    if urlsplit(api_url).scheme not in {"http", "https"}:
        raise ValueError("api_url must be an http or https URL")
    if not 1 <= chunk <= MAX_BATCH:
        raise ValueError(f"chunk must be between 1 and {MAX_BATCH}")
    endpoint = api_url.rstrip("/") + "/v1/transactions/batch"
    totals: Counter[str] = Counter()
    for path in batch_files:
        for rows in batched(_read(path), chunk):
            reply = _post_json(endpoint, {"transactions": [to_payload(r) for r in rows]}, timeout)
            totals.update(reply["counts"])
    return totals
