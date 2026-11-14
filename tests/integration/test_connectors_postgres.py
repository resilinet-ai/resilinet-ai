"""PostgreSQL connector against a real PostgreSQL 16 (B-13: FR-02.1, FR-02.3)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from rai.connectors import ConnectorError, connector
from rai.connectors.postgres import PostgresConnector
from rai.validation.contract import parse_contract
from rai.validation.engine import validate
from rai.validation.verdict import decide

DSN = os.environ.get("RAI_TEST_DSN", "")
if os.environ.get("CI") == "true" and not DSN:  # never skip silently in CI
    raise RuntimeError("RAI_TEST_DSN must be set in CI")
pytestmark = pytest.mark.skipif(not DSN, reason="RAI_TEST_DSN not set")

SCHEMA = "connector_test"
SECRET_ENV = "RAI_CONNECTOR_TEST_PASSWORD"  # noqa: S105 - a variable name, not a value

CONTRACT = parse_contract(
    """
contract: payments
version: 1
primary_key: [id]
columns:
  id:     { type: uuid, required: true }
  amount: { type: "decimal(14,2)", min: 0.01 }
  ok:     { type: boolean }
  day:    { type: date }
  at:     { type: timestamp, required: true }
  note:   { type: string }
"""
)


@pytest.fixture(scope="module")
def table() -> Iterator[None]:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(
            f"CREATE TABLE {SCHEMA}.payments (id uuid PRIMARY KEY, amount numeric(14,2), "
            "ok boolean, day date, at timestamptz NOT NULL, note text)"
        )
        conn.execute(
            f"INSERT INTO {SCHEMA}.payments VALUES "
            "('196c1a05-f633-49ec-a32d-c3901c963dda', 10.50, true, '2026-01-31', "
            " '2026-01-01 02:00:00.01+02', 'café, \"quoted\"'), "
            "('9f5b3b85-0f08-4acc-bce8-74e871374ace', NULL, NULL, NULL, "
            " '2026-01-01 00:00:00+00', NULL)"
        )
        conn.execute(f'CREATE TABLE {SCHEMA}."odd ""name" (x int)')
    yield
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def config(**overrides: Any) -> dict[str, Any]:
    parts = conninfo_to_dict(DSN)
    base = {
        "name": "ledger",
        "environment": "SANDBOX-01",
        "type": "postgres",
        "host": parts.get("host", "localhost"),
        "port": int(parts.get("port", 5432)),
        "database": parts["dbname"],
        "user": parts["user"],
        "secret_ref": f"env:{SECRET_ENV}",
        "sslmode": "disable",
        "schema_name": SCHEMA,
    }
    return base | overrides


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch, table: None) -> PostgresConnector:
    monkeypatch.setenv(SECRET_ENV, str(conninfo_to_dict(DSN)["password"]))
    built = connector(config())
    assert isinstance(built, PostgresConnector)
    return built


def test_reads_a_table_as_engine_text(pg: PostgresConnector) -> None:
    frame = pg.read("payments", environment="SANDBOX-01")
    assert frame.columns == ["id", "amount", "ok", "day", "at", "note"]
    first, second = frame.rows(named=True)
    assert first == {
        "id": "196c1a05-f633-49ec-a32d-c3901c963dda",
        "amount": "10.50",
        "ok": "true",
        "day": "2026-01-31",
        "at": "2026-01-01T00:00:00.010000Z",  # converted to UTC
        "note": 'café, "quoted"',
    }
    assert (second["amount"], second["ok"], second["day"], second["note"]) == (None,) * 4
    verdict = decide(validate(CONTRACT, frame))
    assert verdict.passed, verdict.summary()


def test_limit(pg: PostgresConnector) -> None:
    assert pg.read("payments", environment="SANDBOX-01", limit=1).height == 1


def test_sessions_are_read_only(pg: PostgresConnector) -> None:
    with pg.connect() as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert conn.execute("SHOW statement_timeout").fetchone() == ("1min",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(f"DELETE FROM {SCHEMA}.payments")


def test_identifiers_are_quoted_not_injected(pg: PostgresConnector) -> None:
    assert pg.read('odd "name', environment="SANDBOX-01").columns == ["x"]
    with pytest.raises(ConnectorError, match="not found"):
        pg.read("payments; DROP TABLE payments", environment="SANDBOX-01")
    assert pg.read("payments", environment="SANDBOX-01").height == 2


def test_missing_table_and_wrong_environment(pg: PostgresConnector) -> None:
    with pytest.raises(ConnectorError, match=r"connector_test.nope not found"):
        pg.read("nope", environment="SANDBOX-01")
    with pytest.raises(ConnectorError, match="belongs to environment"):
        pg.read("payments", environment="PRODUCTION")


def test_wrong_password_never_appears_in_the_error(
    monkeypatch: pytest.MonkeyPatch, table: None
) -> None:
    wrong = "wrong-pw-5d1c9e"  # test value only
    monkeypatch.setenv(SECRET_ENV, wrong)
    with pytest.raises(ConnectorError) as info:
        connector(config()).read("payments", environment="SANDBOX-01")
    assert "could not connect" in str(info.value)
    assert wrong not in str(info.value)


def test_unresolvable_secret_reference(monkeypatch: pytest.MonkeyPatch, table: None) -> None:
    monkeypatch.delenv(SECRET_ENV, raising=False)
    with pytest.raises(ConnectorError, match=f"environment variable {SECRET_ENV}"):
        connector(config()).read("payments", environment="SANDBOX-01")


def test_describe_holds_only_the_reference(pg: PostgresConnector) -> None:
    described = str(pg.describe())
    assert f"env:{SECRET_ENV}" in described
    assert str(conninfo_to_dict(DSN)["password"]) not in described
