# SandboxBank

SandboxBank is the synthetic payments system that RAI tests against (Spec section 5).
**It holds synthetic data only.** Every name and identifier is generated, and nothing is sampled from real people or businesses.

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
