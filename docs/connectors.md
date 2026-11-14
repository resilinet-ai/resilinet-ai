# Connectors

A connector reads data assets for validation. It is bound to **one environment** and returns every column as text. The rule engine therefore validates a PostgreSQL table, a Parquet file and a CSV file the same way (FR-02.1).

## Configuration

```yaml
# Files: assets are paths relative to root; nothing outside root can be read.
name: sandbox-files
environment: SANDBOX-01
type: csv            # or parquet
root: /data/sandboxbank/feed
```

```yaml
# PostgreSQL: assets are table names in schema_name; sessions are read-only.
name: ledger
environment: SANDBOX-01
type: postgres
host: ledger-db
port: 5432
database: ledger
user: rai_reader
secret_ref: env:LEDGER_DB_PASSWORD          # or file:/run/secrets/ledger_password
sslmode: prefer
schema_name: public
statement_timeout_seconds: 60
```

## Credentials (FR-02.3)

A config holds only a **reference** to a credential, never the credential itself:

- **Accepted references:** `env:NAME` (an environment variable), or `file:/run/secrets/name` (a Docker secret; a Windows drive path also works for development).
- **Rejected configs:** any config key that looks like a credential (`password`, `token`, `dsn`, `api_key` and so on) makes the config invalid, with the message "use secret_ref".
- **When the value is read:** only when a connection opens. A resolved secret prints as `****`, and every connector error is scrubbed of secret values, URL passwords and `password=…` pairs.
- **What gets stored:** `describe()` returns the configuration with the reference, which is safe to store or log.

## How values are rendered as text

| Source type | Text form |
| --- | --- |
| Timestamp with time zone | `2026-01-01T00:00:00.010000Z`, converted to UTC |
| Timestamp without time zone | `2026-01-01T00:00:00.010000`. The engine reports a type error, because a contract timestamp needs a zone. |
| Date | `2026-01-31` |
| Boolean | `true` / `false` |
| Decimal or numeric | exact text, for example `10.50` |
| Null | null (counts as missing) |

## PostgreSQL safety

- **Read-only sessions:** every session starts with `default_transaction_read_only=on` and a statement timeout.
- **No free SQL:** an asset is a table name, and identifiers are quoted with `psycopg.sql`.
- **Plain text out:** data is streamed with `COPY … TO STDOUT` after the values are rendered as text in SQL.
- **Least privilege:** use a database role that can only `SELECT` the tables RAI validates. The read-only session is a second line of defence, not the only one.
