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

## Seeding log (B-30: RPO ground truth)

The ledger is the system under test, so RAI doesn't take its word alone for what was lost (threat T-07). `sandboxbank seed` posts transactions one at a time at a steady rate, for example while a chaos scenario runs. After each answer, and before sending the next write, it appends a line to its own log:

```
python -m uv run sandboxbank seed --data data/sandboxbank --log acks/acks.csv --rate 10 --limit 3000
python -m uv run sandboxbank verify-acks acks/acks.csv
```

- **Columns:** `n, txn_id, outcome, acked, seq, acked_at, chain`.
  - `outcome` is `created`, `replayed`, `conflict`, `rejected`, or `unacknowledged` (a timeout, a refused connection or a 5xx).
  - `acked` is `true` only for `created` and `replayed`: the API promised the write was committed.
- **Each line is fsynced** before the next write is sent, so the log never claims less than the API acknowledged.
- **The log is append-only and hash-chained.** `verify-acks` finds an edited, reordered or truncated line. A restarted seeder continues the same chain.
- **RAI reconciles the acknowledged rows against the ledger** after recovery (see [RPO ground truth](../docs/api.md#chaos-scenarios)). An acknowledged write missing from the ledger is a broken promise, whatever `GET /v1/ledger/head` says.
- **Use transactions the ledger hasn't seen yet,** e.g. a batch that hasn't been posted. A repeated `txn_id` comes back `replayed`, which is acknowledged but proves nothing new.

## Recovery observer (B-44: RTO ground truth)

Protocol H3 checks RAI's RTO against the truth, so the truth can't come from RAI's own probes. `sandboxbank observe` runs in its own container, `rai-observer`, which belongs to no arm and carries no `rai.sandbox` label, so no fault can target it. Every 250 ms, on a schedule kept by its own monotonic clock, it takes one sample and appends it to its own log:

```
docker compose --profile sandbox --profile observer up -d
python -m uv run sandboxbank verify-observations obs/observations.csv
python -m uv run sandboxbank observed-rto obs/observations.csv --onset 2026-11-20T12:00:10Z --reverted 2026-11-20T12:01:10Z
```

- **What one sample checks:**
  - the payments-api's `/readyz`, the way clients reach it;
  - the **consistency query**: `max(seq)` on ledger-db, read directly rather than through Toxiproxy, in a read-only session.

  A sample is healthy when both answer within 2 s, the same timeout RAI's probes use.
- **Columns:** `n, at, api, ledger, seq, healthy, chain`. The `at` column is the wall clock read once at start-up plus the monotonic time since then, so a clock step during a run cannot move it.
- **The schedule never drifts.** A slow sample skips the ticks it missed rather than making them up.
- **The log is append-only and hash-chained,** like the seeding log, and fsynced line by line. A restarted observer continues the same chain.
- **`observed-rto`** applies RAI's restoration rule to the samples: ignore everything before the last revert, then take the first healthy sample from which the system stayed healthy for `--hold` seconds. The default hold is 2 s, which matches RAI's default of 3 probe rounds 1 s apart. Only the clock and the sampling rate differ from RAI's measurement, so any difference is measurement error, not a different definition.
- **Unobserved is not "not restored".** If the observer started after the revert, or its samples have a gap of more than 5 s before restoration, the answer is *unobserved* (exit 1), and `rai-bench h3` excludes that injection rather than scoring it.

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

## Fault paths (B-23)

In Compose, SandboxBank's database traffic goes through Toxiproxy ([`toxiproxy.json`](toxiproxy.json)): `ledger-db-proxy` (payments-api and the ETL to ledger-db) and `analytics-db-proxy` (the ETL to analytics-db). Every service the chaos agent may act on carries `rai.sandbox=true` and a `rai.target` label. See [docs/chaos-agent.md](../docs/chaos-agent.md).

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

## Fraud model (ADR-005)

A gradient-boosted classifier (scikit-learn `HistGradientBoostingClassifier`) that scores each transaction's probability of fraud, so RAI has a real model to evaluate (FR-09.1) and whose scores can drift (FR-09.2). Synthetic data only; read the **[model card](MODEL_CARD.md)** for its data, features, hold-out numbers with their counts, and limitations.

```bash
uv sync --package rai-sandboxbank --extra model          # scikit-learn, only for the model
uv run sandboxbank model score --feed data/slice/feed --batch B000001 --out scores.csv
uv run sandboxbank model card --out sandboxbank/MODEL_CARD.md
uv run sandboxbank model features --feed data/slice/feed --batch B000001 --out features.csv
uv run sandboxbank model serve --port 8081                # the model-api (FR-09.3)
```

- **No model file is shipped.** A pickled model can run code when loaded, so the model is its recipe in `sandboxbank/model.py` (development seeds, features, hyperparameters) and is retrained from it in seconds by every command. The card records the recipe's SHA-256 and a fingerprint of the hold-out scores; a test fails if the published card does not match the recipe.
- **Scores** come out as `txn_id, score, is_fraud`, ready for a RAI drift check on `score` against an earlier batch's scores.
- **Development seeds only**: training seed 31000, batches 0 to 7, hold-out batches 8 and 9. Evaluation seeds belong to the registered benchmark.
- **Features** come out as `txn_id`, the eight named inputs and `is_fraud`. Categories are kept as text and a missing value is an empty cell. RAI's robustness tests perturb this table.
- **The model-api** (`sandboxbank model serve`; compose service `model-api`, profile `model`, port `127.0.0.1:18081`) trains from the recipe at startup, in about 20 seconds; `/readyz` answers 503 until then.
  - `POST /v1/score` takes `{"rows": [{feature: value or null}]}`, at most 5,000 rows, and returns `{"model": {format, recipe_sha256, scikit_learn, sandboxbank}, "scores": [...]}` in order.
  - Every row must name exactly the eight features. A missing value and a category outside the vocabulary are both missing to the model.
  - Nothing is stored or logged. Register it with RAI as an `http` target with `score_url` `http://127.0.0.1:18081/v1/score` (FR-09.3).
- **In the image.** The SandboxBank image installs the `model` extra for the model-api; the payments API and the ETL do not use it.

## Canary customers (FR-05.1)

Fake customers for RAI's privacy sentinel. The feed carries no personal data, so SandboxBank plants a few canaries: realistic-looking personal details linked to real `customer_id`s, loaded into ledger-db's `customers` table, which nothing downstream copies. A canary value found anywhere else (the warehouse, a log, an evidence bundle) is a leak, and never a coincidence: every value is unique.

```bash
uv run sandboxbank canaries write --accounts data/sandboxbank/accounts.csv --seed 7 --out data/canaries
uv run sandboxbank canaries load --dir data/canaries          # needs SANDBOXBANK_LEDGER_DSN
```

- **Impossible by construction.** Emails end in `@example.invalid` (a reserved domain that never resolves; the table refuses any other), phones are in the `555 01xx` range reserved for fiction, and card numbers are in the `400000` test range with a valid Luhn digit, so card detectors still match them. Surnames are built from syllables, never taken from a list of people.
- **`canaries.json` is the answer key**: every canary's needles (marker, email, name, card number with and without spaces, phone with and without punctuation), the values RAI searches for. `needles.csv` holds the same key as a table (`canary, kind, value`), which a RAI `privacy_canary` test reads through a connector. Keep both with the run's inputs, like any answer key. `rai canary scan --key needles.csv` sweeps logs and evidence bundles with either file.
- **Nothing else changes.** Canaries come from their own random stream of the seed, so a dataset and its fixtures stay byte-identical.
