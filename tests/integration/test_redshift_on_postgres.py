"""The Redshift connector against PostgreSQL 16, its stand-in (FR-02.2).

Redshift speaks the PostgreSQL wire protocol. These tests run the connector's own
path (``SET`` after connecting, ``BEGIN READ ONLY``, a server-side cursor) on the
same table as the PostgreSQL connector's tests, and expect the same text. They
cannot show Redshift-only behaviour; a run against a live cluster is still owed.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from rai.connectors import ConnectorError, connector
from rai.connectors.redshift import RedshiftConnector
from rai.validation.engine import validate
from rai.validation.verdict import decide
from tests.integration.test_connectors_postgres import (
    CONTRACT,
    DSN,
    SCHEMA,
    SECRET_ENV,
    config,
    pytestmark,  # noqa: F401 - skipped without RAI_TEST_DSN, like the PostgreSQL tests
    table,  # noqa: F401 - the shared fixture
)


@pytest.fixture
def rs(monkeypatch: pytest.MonkeyPatch, table: None) -> RedshiftConnector:  # noqa: F811
    monkeypatch.setenv(SECRET_ENV, str(conninfo_to_dict(DSN)["password"]))
    built = connector(config(type="redshift"))
    assert isinstance(built, RedshiftConnector)
    return built


def test_reads_the_same_text_as_the_postgres_connector(
    rs: RedshiftConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = rs.read("payments", environment="SANDBOX-01")
    postgres = connector(config()).read("payments", environment="SANDBOX-01")
    assert frame.equals(postgres)
    assert frame.row(0, named=True)["at"] == "2026-01-01T00:00:00.010000Z"
    assert decide(validate(CONTRACT, frame)).passed


def test_limit_and_batches(rs: RedshiftConnector, monkeypatch: pytest.MonkeyPatch) -> None:
    from rai.connectors import redshift

    monkeypatch.setattr(redshift, "FETCH_ROWS", 1)  # more than one fetch
    assert rs.read("payments", environment="SANDBOX-01").height == 2
    assert rs.read("payments", environment="SANDBOX-01", limit=1).height == 1


def test_sessions_are_read_only_with_a_timeout(rs: RedshiftConnector) -> None:
    with rs.connect() as conn:
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert conn.execute("SHOW statement_timeout").fetchone() == ("1min",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(f"DELETE FROM {SCHEMA}.payments")


def test_identifiers_are_quoted_and_a_missing_table_is_an_error(rs: RedshiftConnector) -> None:
    assert rs.read('odd "name', environment="SANDBOX-01").columns == ["x"]
    with pytest.raises(ConnectorError, match="not found"):
        rs.read("payments; DROP TABLE payments", environment="SANDBOX-01")
    with pytest.raises(ConnectorError, match="belongs to environment"):
        rs.read("payments", environment="PRODUCTION")
