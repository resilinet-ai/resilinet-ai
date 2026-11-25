"""Arm B1, failure-scenario half: the integration tests a team reruns every 60 s.

Protocol section 3: B1 "reruns integration tests every 60 s" during the chaos benchmark.
Written from SandboxBank's documentation (``sandboxbank/README.md``) before any run,
and **read-only** by the author's decision (2026-11-25): these tests write nothing, so
the seeding log's ground truth for RPO stays B1-free.

Each test checks one component the README documents. The harness
(``rai_bench.live.B1Live``) maps a failing test to the component it names:

* ``test_payments_api_is_ready``: payments-api ``/readyz`` answers 200;
* ``test_ledger_head_is_readable_and_never_falls``: ``/v1/ledger/head`` answers, and its
  ``seq`` is not lower than at the previous run (the ledger is append-only);
* ``test_warehouse_answers``: analytics-db answers a query;
* ``test_warehouse_keeps_up_with_the_ledger``: the warehouse's newest row was committed
  at most 5 minutes before the ledger's newest (the ETL runs every 60 s, so a suite
  allows a few cycles);
* ``test_feature_table_keeps_up_with_the_warehouse``: the feature table is at most 5
  minutes behind the warehouse.

Environment: ``RAI_B1_PAYMENTS`` (payments-api base URL), ``RAI_B1_ANALYTICS_DSN``
(analytics-db, opened read-only), and ``RAI_B1_STATE`` (a JSON file kept between runs
for the previous ledger ``seq``). Frozen after the external review, with the rest of B1.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

PAYMENTS = os.environ.get("RAI_B1_PAYMENTS", "http://127.0.0.1:18080").rstrip("/")
ANALYTICS_DSN = os.environ.get("RAI_B1_ANALYTICS_DSN", "")
STATE = Path(os.environ.get("RAI_B1_STATE", "b1-live-state.json"))
TIMEOUT = 5.0
MAX_LAG = timedelta(minutes=5)


def get_json(path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(PAYMENTS + path, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, None


def query(sql: str) -> Any:
    import psycopg

    if not ANALYTICS_DSN:
        pytest.fail("RAI_B1_ANALYTICS_DSN is not set")
    options = "-c default_transaction_read_only=on -c statement_timeout=5000"
    with psycopg.connect(ANALYTICS_DSN, connect_timeout=5, options=options) as conn:
        row = conn.execute(sql).fetchone()  # type: ignore[arg-type]
    return row[0] if row else None


def parse(moment: Any) -> datetime:
    return moment if isinstance(moment, datetime) else datetime.fromisoformat(
        str(moment).replace("Z", "+00:00"))  # fmt: skip


def test_payments_api_is_ready() -> None:
    status, _ = get_json("/readyz")
    assert status == 200, f"payments-api /readyz answered {status}"


def test_ledger_head_is_readable_and_never_falls() -> None:
    status, head = get_json("/v1/ledger/head")
    assert status == 200, f"/v1/ledger/head answered {status}"
    assert isinstance(head, dict), "/v1/ledger/head did not answer with the head"
    seq = int(head.get("seq") or 0)
    previous = json.loads(STATE.read_text(encoding="utf-8")) if STATE.is_file() else {}
    STATE.write_text(json.dumps(previous | {"ledger_seq": max(seq, previous.get("ledger_seq", 0))}),
                     encoding="utf-8")  # fmt: skip
    assert seq >= previous.get("ledger_seq", 0), (
        f"ledger seq fell from {previous['ledger_seq']} to {seq}")  # fmt: skip


def test_warehouse_answers() -> None:
    assert query("SELECT count(*) FROM warehouse.fact_transaction") is not None


def test_warehouse_keeps_up_with_the_ledger() -> None:
    status, head = get_json("/v1/ledger/head")
    if status != 200 or not isinstance(head, dict) or not head.get("committed_at"):
        pytest.skip("the ledger head is unavailable: reported by its own test")
    newest = query("SELECT max(committed_at) FROM warehouse.fact_transaction")
    assert newest is not None, "the warehouse has no rows"
    lag = parse(head["committed_at"]) - parse(newest)
    assert lag <= MAX_LAG, f"the warehouse is {lag} behind the ledger"


def test_feature_table_keeps_up_with_the_warehouse() -> None:
    lag = query(
        "SELECT (SELECT max(committed_at) FROM warehouse.fact_transaction) - "
        "(SELECT committed_at FROM warehouse.fact_transaction WHERE txn_id = "
        " (SELECT txn_id FROM features.fraud_features ORDER BY ledger_seq DESC LIMIT 1) LIMIT 1)"
    )
    assert lag is not None, "the feature table has no rows"
    assert lag <= MAX_LAG, f"the feature table is {lag} behind the warehouse"
