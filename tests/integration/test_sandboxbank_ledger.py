"""Payments-api and ledger-db against a real PostgreSQL 16 (B-03).

Needs SANDBOXBANK_TEST_DSN, for example
postgresql://ledger:<password>@127.0.0.1:55433/ledger_test (see sandboxbank/README.md).
Skipped when it is not set; CI always sets it.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient

from sandboxbank.api import create_app
from sandboxbank.cli import main
from sandboxbank.dataset import write_dataset
from sandboxbank.generator import GeneratorConfig
from sandboxbank.ledger import TransactionIn, ledger_head, migrate, post_transactions
from sandboxbank.loader import load_reference, post_batches

DSN = os.environ.get("SANDBOXBANK_TEST_DSN", "")
if os.environ.get("CI") == "true" and not DSN:  # never skip silently in CI
    raise RuntimeError("SANDBOXBANK_TEST_DSN must be set in CI")
pytestmark = pytest.mark.skipif(not DSN, reason="SANDBOXBANK_TEST_DSN not set")

ACTIVE, CLOSED, MERCHANT = "ACC-0000001", "ACC-0000002", "MER-000001"


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(DSN, autocommit=True) as connection:
        migrate(connection)
        connection.execute(
            "TRUNCATE ledger_transactions, accounts, merchants RESTART IDENTITY CASCADE"
        )
        connection.execute(
            "INSERT INTO accounts VALUES "
            "(%s, 'CUS-0000001', 'checking', 'USD', 'active', '2020-01-01', 5000.00), "
            "(%s, 'CUS-0000001', 'savings', 'USD', 'closed', '2020-01-01', 2500.00)",
            (ACTIVE, CLOSED),
        )
        connection.execute(
            "INSERT INTO merchants VALUES (%s, 'SBX Merchant 000001', '5411', 'grocery', 'US')",
            (MERCHANT,),
        )
        yield connection


def txn(**overrides: Any) -> TransactionIn:
    base: dict[str, Any] = {
        "txn_id": str(uuid4()),
        "batch_id": "B000000",
        "account_id": ACTIVE,
        "merchant_id": MERCHANT,
        "channel": "card_present",
        "direction": "debit",
        "amount": "25.00",
        "currency": "USD",
        "status": "posted",
        "created_at": "2026-01-01T00:00:01.000Z",
        "is_fraud": False,
    }
    return TransactionIn.model_validate(base | overrides)


def ledger_count(connection: psycopg.Connection[Any]) -> int:
    row = connection.execute("SELECT count(*) FROM ledger_transactions").fetchone()
    return row[0] if row else 0


# ---- idempotent posting -----------------------------------------------------


def test_migrate_is_idempotent(conn: psycopg.Connection[Any]) -> None:
    migrate(conn)
    migrate(conn)


def test_create_then_replay_returns_the_same_ledger_row(conn: psycopg.Connection[Any]) -> None:
    first = txn()
    (created,) = post_transactions(conn, [first])
    (replayed,) = post_transactions(conn, [first])
    assert created.outcome == "created"
    assert replayed.outcome == "replayed"
    assert replayed.seq == created.seq
    assert ledger_count(conn) == 1


def test_same_txn_id_with_different_content_is_a_conflict(conn: psycopg.Connection[Any]) -> None:
    original = txn()
    post_transactions(conn, [original])
    changed = txn(txn_id=str(original.txn_id), amount="26.00")
    (result,) = post_transactions(conn, [changed])
    assert result.outcome == "conflict"
    row = conn.execute("SELECT amount FROM ledger_transactions").fetchone()
    assert row is not None
    assert str(row[0]) == "25.00"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"account_id": "ACC-0009999"}, "unknown account_id"),
        ({"account_id": CLOSED, "channel": "ach", "merchant_id": None}, "account is closed"),
        ({"amount": "5000.01"}, "amount exceeds account txn_limit"),
        ({"merchant_id": "MER-999999"}, "unknown merchant_id"),
    ],
)
def test_business_rule_failures_are_rejected_without_writing(
    conn: psycopg.Connection[Any], overrides: dict[str, Any], reason: str
) -> None:
    (result,) = post_transactions(conn, [txn(**overrides)])
    assert (result.outcome, result.reason) == ("rejected", reason)
    assert ledger_count(conn) == 0


def test_repeats_inside_one_request(conn: psycopg.Connection[Any]) -> None:
    a = txn()
    a_changed = txn(txn_id=str(a.txn_id), amount="1.00")
    bad = txn(account_id="ACC-0009999")
    results = post_transactions(conn, [a, a, a_changed, bad, bad])
    assert [r.outcome for r in results] == [
        "created",
        "replayed",
        "conflict",
        "rejected",
        "rejected",
    ]
    assert results[1].seq == results[0].seq
    assert ledger_count(conn) == 1


def test_concurrent_retries_create_exactly_one_row(conn: psycopg.Connection[Any]) -> None:
    shared = txn()

    def post_once(_: int) -> str:
        with psycopg.connect(DSN) as own:
            return post_transactions(own, [shared])[0].outcome

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = Counter(pool.map(post_once, range(16)))
    assert outcomes == Counter({"created": 1, "replayed": 15})
    assert ledger_count(conn) == 1


def test_batch_limit(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(ValueError, match="at most"):
        post_transactions(conn, [txn() for _ in range(1001)])


def test_ledger_is_append_only(conn: psycopg.Connection[Any]) -> None:
    post_transactions(conn, [txn()])
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        conn.execute("UPDATE ledger_transactions SET amount = 1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
        conn.execute("DELETE FROM ledger_transactions")


def test_ledger_head_tracks_the_last_commit(conn: psycopg.Connection[Any]) -> None:
    assert ledger_head(conn).count == 0
    assert ledger_head(conn).seq is None
    results = post_transactions(conn, [txn(), txn(), txn()])
    head = ledger_head(conn)
    assert head.count == 3
    assert head.seq == max(r.seq or 0 for r in results)
    assert head.committed_at is not None


# ---- HTTP API ------------------------------------------------------------


@pytest.fixture
def client(conn: psycopg.Connection[Any]) -> Iterator[TestClient]:
    with TestClient(create_app(DSN)) as test_client:
        yield test_client


def body(transaction: TransactionIn) -> dict[str, Any]:
    return transaction.model_dump(mode="json")


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_single_post_status_codes(client: TestClient) -> None:
    one = txn()
    assert client.post("/v1/transactions", json=body(one)).status_code == 201
    assert client.post("/v1/transactions", json=body(one)).status_code == 200
    changed = body(one) | {"amount": "30.00"}
    assert client.post("/v1/transactions", json=changed).status_code == 409
    unknown = body(txn(account_id="ACC-0009999"))
    reply = client.post("/v1/transactions", json=unknown)
    assert reply.status_code == 422
    assert reply.json()["reason"] == "unknown account_id"
    assert client.post("/v1/transactions", json={"txn_id": "x"}).status_code == 422


def test_batch_endpoint_and_reads(client: TestClient) -> None:
    items = [txn(), txn(), txn(account_id="ACC-0009999")]
    reply = client.post("/v1/transactions/batch", json={"transactions": [body(t) for t in items]})
    assert reply.status_code == 200
    assert reply.json()["counts"] == {"created": 2, "replayed": 0, "conflict": 0, "rejected": 1}

    found = client.get(f"/v1/transactions/{items[0].txn_id}").json()
    assert UUID(found["txn_id"]) == items[0].txn_id
    assert found["amount"] == "25.00"
    assert client.get(f"/v1/transactions/{uuid4()}").status_code == 404
    head = client.get("/v1/ledger/head").json()
    assert head["count"] == 2
    assert client.post("/v1/transactions/batch", json={"transactions": []}).status_code == 422


def test_app_needs_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOXBANK_LEDGER_DSN", raising=False)
    with pytest.raises(RuntimeError, match="SANDBOXBANK_LEDGER_DSN"), TestClient(create_app()):
        pass


# ---- end to end: generator -> loader -> payments-api -> ledger -------------


@pytest.fixture
def live_api(conn: psycopg.Connection[Any]) -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(DSN), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise TimeoutError("payments-api did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_generated_dataset_posts_once_and_replays_cleanly(
    conn: psycopg.Connection[Any],
    live_api: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn.execute("TRUNCATE ledger_transactions, accounts, merchants RESTART IDENTITY CASCADE")
    config = GeneratorConfig(seed=3, accounts=40, merchants=8, batches=2, rows_per_batch=300)
    write_dataset(config, tmp_path)

    assert load_reference(conn, tmp_path) == {"accounts": 40, "merchants": 8}
    assert load_reference(conn, tmp_path) == {"accounts": 0, "merchants": 0}

    files = sorted((tmp_path / "transactions_raw").glob("B*.csv"))
    first = post_batches(live_api, files, chunk=250)
    assert first == Counter({"created": 600, "replayed": 0, "conflict": 0, "rejected": 0})
    retry = post_batches(live_api, files, chunk=250)  # e.g. a feed replayed after failover
    assert retry == Counter({"created": 0, "replayed": 600, "conflict": 0, "rejected": 0})
    assert ledger_count(conn) == 600

    monkeypatch.setenv("SANDBOXBANK_LEDGER_DSN", DSN)
    assert main(["load-reference", "--data", str(tmp_path)]) == 0
    assert "accounts: 0 new" in capsys.readouterr().out
    assert main(["post", "--data", str(tmp_path), "--api", live_api, "--batch", "B000001"]) == 0
    assert "replayed: 300" in capsys.readouterr().out
