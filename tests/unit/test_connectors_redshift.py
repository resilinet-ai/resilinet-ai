"""The Redshift connector without a database (FR-02.2): its configuration, and that it
never uses what Redshift lacks (startup options, ``COPY ... TO STDOUT``)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from rai.connectors import ConnectorError, connector, parse_config
from rai.connectors.base import RedshiftConfig
from rai.connectors.redshift import RedshiftConnector

FAKE = "not-a-real-credential-3c9d"  # test value only
BASE: dict[str, Any] = {
    "name": "warehouse", "environment": "SANDBOX-01", "type": "redshift",
    "host": "example.abc123.us-east-1.redshift.amazonaws.com", "database": "dev",
    "user": "rai_reader", "secret_ref": "env:RAI_REDSHIFT_TEST_PASSWORD",
}  # fmt: skip


def test_defaults_are_redshifts_port_and_tls() -> None:
    parsed = parse_config(BASE)
    assert isinstance(parsed, RedshiftConfig)
    assert (parsed.port, parsed.sslmode, parsed.schema_name) == (5439, "require", "public")
    assert isinstance(connector(BASE), RedshiftConnector)


@pytest.mark.parametrize("mode", ["prefer", "allow"])
def test_a_silent_fallback_to_plain_text_is_refused(mode: str) -> None:
    with pytest.raises(ConnectorError, match="sslmode"):
        parse_config(BASE | {"sslmode": mode})


def test_credentials_only_by_reference() -> None:
    with pytest.raises(ConnectorError, match="use secret_ref"):
        parse_config(BASE | {"password": FAKE})
    with pytest.raises(ConnectorError, match="secret_ref"):
        parse_config(BASE | {"secret_ref": FAKE})


class _Recorder:
    """Stands in for a psycopg connection: records what the connector sends."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.autocommit = True
        self.read_only = False

    def execute(self, query: Any, *args: Any) -> None:
        self.sent.append(query.as_string(None) if hasattr(query, "as_string") else str(query))

    def close(self) -> None:
        pass


def test_the_session_is_set_up_without_startup_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAI_REDSHIFT_TEST_PASSWORD", FAKE)
    calls: list[dict[str, Any]] = []
    recorder = _Recorder()

    def fake_connect(**kwargs: Any) -> _Recorder:
        calls.append(kwargs)
        return recorder

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    built = connector(BASE | {"statement_timeout_seconds": 30})
    assert isinstance(built, RedshiftConnector)
    conn = built.connect()
    (kwargs,) = calls
    assert "options" not in kwargs  # Redshift takes no -c settings at startup
    assert (kwargs["port"], kwargs["sslmode"]) == (5439, "require")
    assert recorder.sent == ["SET statement_timeout TO 30000"]
    assert (conn.autocommit, conn.read_only) == (False, True)


def test_a_failed_connection_never_shows_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAI_REDSHIFT_TEST_PASSWORD", FAKE)

    def refuse(**kwargs: Any) -> None:
        raise psycopg.OperationalError(f"password authentication failed: password={FAKE}")

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(ConnectorError) as info:
        connector(BASE).read("t", environment="SANDBOX-01")
    assert "could not connect" in str(info.value)
    assert FAKE not in str(info.value)


def test_the_reader_never_streams_with_copy() -> None:
    import inspect

    from rai.connectors import redshift

    source = inspect.getsource(redshift.RedshiftConnector)
    assert "copy(" not in source.lower()
    assert "options=" not in source
