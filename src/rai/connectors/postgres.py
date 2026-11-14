"""PostgreSQL connector (FR-02.1, FR-02.3).

Safety by construction:

* **Read-only:** every session starts with ``default_transaction_read_only=on``
  and a statement timeout, so even a bug cannot write to the source.
* **Tables only:** an asset is a table name in the configured schema. There is
  no free SQL; identifiers are quoted with ``psycopg.sql``.
* **Credentials by reference:** the password comes from ``secret_ref`` when the
  connection opens and is scrubbed from every error message.

Values are rendered in the engine's text forms in SQL (``to_char`` for
timestamps in UTC, ``true``/``false`` for booleans), then streamed out with
``COPY ... TO STDOUT`` and read as text.
"""

from __future__ import annotations

import io
from typing import Any

import polars as pl
import psycopg
from psycopg import sql

from rai.connectors.base import Connector, ConnectorError, PostgresConfig
from rai.connectors.secrets import Secret, SecretError, SecretRef

_TIMESTAMPTZ = 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
_TIMESTAMP = 'YYYY-MM-DD"T"HH24:MI:SS.US'


class PostgresConnector(Connector):
    config: PostgresConfig

    def _secret(self) -> Secret:
        try:
            return SecretRef.parse(self.config.secret_ref).resolve()
        except SecretError as error:
            raise ConnectorError(f"connector {self.config.name!r}: {error}") from None

    def connect(self) -> psycopg.Connection[Any]:
        """A read-only session with a statement timeout."""
        secret = self._secret()
        timeout_ms = self.config.statement_timeout_seconds * 1000
        try:
            return psycopg.connect(
                host=self.config.host,
                port=self.config.port,
                dbname=self.config.database,
                user=self.config.user,
                password=secret.reveal(),
                sslmode=self.config.sslmode,
                connect_timeout=10,
                application_name="rai-connector",
                options=f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}",
            )
        except psycopg.Error as error:
            raise ConnectorError(
                f"connector {self.config.name!r} could not connect: {error}", secret
            ) from None

    def _columns(self, conn: psycopg.Connection[Any], table: str) -> list[tuple[str, str]]:
        rows = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (self.config.schema_name, table),
        ).fetchall()
        if not rows:
            raise ConnectorError(f"table {self.config.schema_name}.{table} not found")
        return [(str(name), str(data_type)) for name, data_type in rows]

    @staticmethod
    def _as_text(name: str, data_type: str) -> sql.Composable:
        column = sql.Identifier(name)
        match data_type:
            case "timestamp with time zone":
                expr = sql.SQL("to_char({} AT TIME ZONE 'UTC', {})").format(
                    column, sql.Literal(_TIMESTAMPTZ)
                )
            case "timestamp without time zone":
                expr = sql.SQL("to_char({}, {})").format(column, sql.Literal(_TIMESTAMP))
            case "date":
                expr = sql.SQL("to_char({}, 'YYYY-MM-DD')").format(column)
            case "boolean":
                expr = sql.SQL("CASE WHEN {0} THEN 'true' WHEN NOT {0} THEN 'false' END").format(
                    column
                )
            case _:
                expr = sql.SQL("{}::text").format(column)
        return sql.SQL("{} AS {}").format(expr, column)

    def _read(self, asset: str, limit: int | None) -> pl.DataFrame:
        with self.connect() as conn:
            columns = self._columns(conn, asset)
            query = sql.SQL("SELECT {} FROM {}.{}").format(
                sql.SQL(", ").join(self._as_text(n, t) for n, t in columns),
                sql.Identifier(self.config.schema_name),
                sql.Identifier(asset),
            )
            if limit is not None:
                query = sql.SQL("{} LIMIT {}").format(query, sql.Literal(limit))
            buffer = io.BytesIO()
            try:
                with conn.cursor().copy(
                    sql.SQL("COPY ({}) TO STDOUT WITH (FORMAT csv, HEADER true)").format(query)
                ) as copy:
                    for chunk in copy:
                        buffer.write(chunk)
            except psycopg.Error as error:
                raise ConnectorError(f"could not read {asset!r}: {error}") from None
        buffer.seek(0)
        names = [name for name, _ in columns]
        if buffer.getbuffer().nbytes == 0:  # pragma: no cover - COPY always sends a header
            return pl.DataFrame({name: [] for name in names}, schema=dict.fromkeys(names, pl.Utf8))
        return pl.read_csv(buffer, infer_schema=False)
