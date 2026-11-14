# SandboxBank

SandboxBank is the synthetic payments system that RAI tests against (Spec section 5).
**It holds synthetic data only.** Every name and identifier is generated, and nothing is sampled from real people or businesses.

## Quick start: generate, load and post

1. Copy `.env.example` to `.env` and choose your own passwords.
2. Run these from the repository root. The `export` reads `.env`, so the CLI's connection string gets the password you chose (in Command Prompt, set `SANDBOXBANK_LEDGER_DSN` by hand instead):

```
docker compose --profile sandbox up -d --build --wait ledger-db analytics-db payments-api etl-job
set -a; . ./.env; set +a
python -m uv run sandboxbank generate --seed 42 --out data/sandboxbank --batches 2
python -m uv run sandboxbank load-reference --data data/sandboxbank
python -m uv run sandboxbank post --data data/sandboxbank
```

Running `post` a second time is safe. Every row comes back `replayed`, and the ledger doesn't change.

## Payments API and ledger (B-03, pipeline P1)

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

## Settlement ETL and analytics warehouse (B-04, pipeline P2)

`etl-job` is a long-running service. It copies the ledger into the `warehouse` schema of `analytics-db` (published on `127.0.0.1:55434`) every `ETL_INTERVAL_SECONDS` (default 60). Each cycle does three things:

1. **Dimensions.** Copies accounts and merchants into `dim_account` and `dim_merchant`. It adds only natural keys the warehouse doesn't have yet.
2. **Facts.** Copies posted ledger rows into `fact_transaction`, in chunks taken in ledger `seq` order. Each chunk's rows, its watermark move and its business dates commit together.
3. **Settlement.** Rebuilds `settlement_account_daily` and `settlement_merchant_daily` for each queued business date (UTC).

**Recovery behaviour, for the Protocol's chaos scenarios (SBX-ETL):**
- Stopped or restarted mid-load (S12, S16): the job resumes from the last committed chunk with nothing lost or doubled. Compose restarts it automatically.
- Database unreachable (S04, S07, S09): the job logs `database_unavailable` and retries every interval until the fault clears.
- Commit timing: ledger `seq` is assigned at insert, not commit, so the job only reads rows committed at least 5 seconds ago (`--lag`). That stops it skipping a slow transaction.
- Logging: `etl_runs` records completed cycles only. If a cycle is killed, the chunks it committed show up in `etl_watermark` but not in the run log.

**Unenforced keys.** As in many real warehouses, natural keys are **not** enforced unique. This is deliberate: defects such as D12 (a duplicated dimension key inflating settlement) must be able to happen, or RAI can't be scored on catching them.

**Run one cycle by hand**, after `set -a; . ./.env; set +a`:

```
python -m uv run sandboxbank etl --once --lag 0
```

## Known-defect fixtures and the sealed answer key (B-07)

```
python -m uv run sandboxbank fixtures --data-seed 42 --injector-seed 43 --out data/fixtures
python -m uv run sandboxbank seal data/fixtures --expect <sha256 committed earlier>
```

This builds one Protocol §4 fixture set for pipeline P1:
- **180 batches**: 12 for each of the 12 defect classes, spread evenly across that class's rates, plus 36 clean control batches;
- **100,000 rows per batch**, in a seeded random order;
- **batch 0 is always clean**, because classes D02 and D11 need a previous batch.

At the full protocol size it takes a few minutes and about 2 GB. Use `--rows`, `--per-class` and `--clean` for small development sets.

| Folder | Who reads it |
| --- | --- |
| `feed/` | The arms under test: reference data, `transactions_raw/Bnnnnnn.csv`, and `merchants_by_batch/` for D12 |
| `answer_key/ground_truth.jsonl` | Scoring only. One line per batch, clean batches included: class, rate, rows changed (0-based rows of the delivered file), columns, keys, window start and onset |
| `answer_key/SEAL` | The SHA-256 of the answer key. **Commit it publicly before any arm runs** (Protocol §8) |

**Seed rules (SBX-INJECT):**
- `--data-seed` drives the generator and `--injector-seed` drives the defects. They must differ and be in the same protocol range: development 0–99,999, evaluation 100,000–199,999.
- The defects come only from the injector, never from the generator, so a clean batch is clean by construction.

**Defect rules:**
- D10 uses a 1-hour freshness window.
- In D09's float variant, values that are exact in binary (such as 12.25) print unchanged, as a real float pipeline would write them.

## Integration tests

They need a real PostgreSQL:

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
