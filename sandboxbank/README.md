# SandboxBank

SandboxBank is the synthetic payments system that RAI tests against (Spec section 5).
**It holds synthetic data only.** Every name and identifier is generated, and nothing is sampled from real people or businesses.

## Quick start: generate, load and post

1. Copy `.env.example` to `.env` and choose your own passwords.
2. Run these from the repository root. The `export` reads `.env`, so the CLI's connection string gets the password you chose (in Command Prompt, set `SANDBOXBANK_LEDGER_DSN` by hand instead):

```
docker compose --profile sandbox up -d --build --wait ledger-db payments-api
set -a; . ./.env; set +a
python -m uv run sandboxbank generate --seed 42 --out data/sandboxbank --batches 2
python -m uv run sandboxbank load-reference --data data/sandboxbank
python -m uv run sandboxbank post --data data/sandboxbank
```

Running `post` a second time is safe. Every row comes back `replayed`, and the ledger doesn't change.

## Payments API and ledger (B-03)

`payments-api` (FastAPI) writes to `ledger-db` (PostgreSQL 16). Both run on `sandbox_net` and are published only on `127.0.0.1`: the API on port 18080 and the database on 55432. The schema is in `src/sandboxbank/ledger/schema.sql` and is applied automatically when the API starts.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/transactions` | Post one transaction. Returns **201** created, **200** replayed, **409** conflict or **422** rejected. |
| `POST /v1/transactions/batch` | Post up to 1,000 transactions. Returns one outcome per row, plus counts. |
| `GET /v1/transactions/{txn_id}` | Read one ledger row. |
| `GET /v1/ledger/head` | The last committed `seq` and `committed_at`, and the row count. B-26 measures RPO from this. |
| `GET /healthz`, `GET /readyz` | Liveness, and readiness (which checks the database). |

**Idempotency (SBX-LEDGER).** `txn_id` is the idempotency key, and the API compares a fingerprint of each request's normalised content.
- An identical repeat is a *replay*: it returns the original `seq`.
- The same `txn_id` with different content is a *conflict*, and nothing changes.
- A request that fails a business rule is *rejected* without writing anything. The rules are: unknown or inactive account, amount over the account limit, or unknown merchant.

Concurrent retries of the same `txn_id` create exactly one row. The ledger is append-only, and a trigger refuses any `UPDATE` or `DELETE`.

**Integration tests** need a real PostgreSQL:

```
make test-db        # or the docker run command in the Makefile
SANDBOXBANK_TEST_DSN=postgresql://ledger:sbx-test@127.0.0.1:55433/ledger_test python -m uv run pytest
```

Without `SANDBOXBANK_TEST_DSN`, these tests are skipped locally. CI always runs them against a PostgreSQL service container.

## Generate data (B-02)

```
python -m uv run sandboxbank generate --seed 42 --out data/sandboxbank --batches 3
python -m uv run sandboxbank verify data/sandboxbank
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--seed` | required | 0–99,999 for development, 100,000–199,999 for evaluation. Any other seed is rejected. |
| `--accounts` | 5,000 | Rows in `accounts` |
| `--merchants` | 500 | Rows in `merchants` |
| `--batches` | 1 | Number of `transactions_raw` micro-batches |
| `--rows` | 100,000 | Rows per batch (Protocol batch size) |
| `--window` | 900 | Seconds each batch covers, starting 2026-01-01T00:00:00Z |

The output directory holds `accounts.csv`, `merchants.csv`, `transactions_raw/B000000.csv` and the files after it, plus `manifest.json`. The manifest records the seed, its purpose, the config, and the row count, size and SHA-256 of each file. `data/` is git-ignored.

**Reproducibility (NFR-REP-1).** The same seed and options give byte-identical files on any platform, including the manifest:
- every random stream is derived from the seed by SHA-256;
- draws use only integer and table lookups;
- no wall-clock time is recorded.

Any batch can also be regenerated on its own. A golden-hash test in CI enforces all of this.

## Schema v1

Defined in `src/sandboxbank/schema.py`. Changing a column is a breaking change: bump `SCHEMA_VERSION` and log it in `docs/decision-log.md`.

**accounts:** `account_id`, `customer_id`, `account_type` (checking, savings or business), `currency`, `status` (active, dormant or closed), `opened_on`, `txn_limit`.

**merchants:** `merchant_id`, `name`, `mcc`, `category`, `country`.

**transactions_raw:** `txn_id` (UUID), `batch_id`, `account_id`, `merchant_id` (empty for ach, wire and p2p), `channel`, `direction`, `amount`, `currency`, `status`, `created_at` (UTC, ms), `is_fraud`.

Generated data is clean by construction. The tests check that it holds the invariants the defect catalogue breaks:

- `txn_id` is unique within and across batches;
- every `account_id` exists and is active;
- each amount is greater than 0 and no more than the account's `txn_limit`;
- each `created_at` falls inside its batch window;
- the column set is fixed.

Defects are added later, by the separate injector (B-07), never by the generator.

About 0.2% of transactions carry a synthetic fraud label. Fraud rows lean toward e-commerce, high-risk merchant categories and larger amounts, which gives the P3 fraud feature pipeline real signal to learn from.
