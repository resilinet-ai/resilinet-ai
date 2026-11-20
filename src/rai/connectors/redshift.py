"""Amazon Redshift connector (FR-02.2, FR-02.3).

Redshift speaks the PostgreSQL wire protocol, so this connector reuses the PostgreSQL
connector's table lookup and its text rendering (``to_char`` for timestamps in UTC,
``true``/``false`` for booleans, ``::text`` for the rest). It differs where Redshift
does:

* **No startup options.** Redshift does not take ``-c`` settings when a session opens,
  so the statement timeout is set with ``SET`` once connected, and the session is
  read-only by opening every transaction with ``BEGIN READ ONLY``.
* **No ``COPY ... TO STDOUT``.** Redshift's ``COPY`` only loads data, so rows are read
  through a server-side cursor in batches of ``FETCH_ROWS``. Redshift materialises a
  cursor's result on its leader node, so very large tables are better read with
  ``limit`` or from an export.
* **TLS by default** (``sslmode: require``); see :class:`RedshiftConfig`.

Authentication is a database user and password by reference (``secret_ref``). IAM
authentication and temporary credentials are not supported yet.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import psycopg
from psycopg import sql

from rai.connectors.base import ConnectorError
from rai.connectors.postgres import PostgresConnector

FETCH_ROWS = 10_000


class RedshiftConnector(PostgresConnector):
    def connect(self) -> psycopg.Connection[Any]:
        """A read-only session with a statement timeout, set after connecting."""
        secret = self._secret()
        timeout_ms = self.config.statement_timeout_seconds * 1000
        try:
            conn = psycopg.connect(
                host=self.config.host,
                port=self.config.port,
                dbname=self.config.database,
                user=self.config.user,
                password=secret.reveal(),
                sslmode=self.config.sslmode,
                connect_timeout=10,
                application_name="rai-connector",
                autocommit=True,
            )
        except psycopg.Error as error:
            raise ConnectorError(
                f"connector {self.config.name!r} could not connect: {error}", secret
            ) from None
        try:
            conn.execute(sql.SQL("SET statement_timeout TO {}").format(sql.Literal(timeout_ms)))
        except psycopg.Error as error:
            conn.close()
            raise ConnectorError(f"connector {self.config.name!r}: {error}", secret) from None
        conn.autocommit = False
        conn.read_only = True  # every transaction opens with BEGIN READ ONLY
        return conn

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
            rows: list[tuple[Any, ...]] = []
            try:
                with conn.cursor(name="rai_read") as cursor:
                    cursor.execute(query)
                    while batch := cursor.fetchmany(FETCH_ROWS):
                        rows.extend(batch)
            except psycopg.Error as error:
                raise ConnectorError(f"could not read {asset!r}: {error}") from None
        names = [name for name, _ in columns]
        return pl.DataFrame(rows, schema=dict.fromkeys(names, pl.Utf8), orient="row")
