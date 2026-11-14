# Resilinet-AI (RAI)

**An open reference framework for proving that AI-enabled financial data pipelines catch bad data and recover from failures within stated targets, before changes reach production.**

RAI combines four things that smaller banks, credit unions and fintechs rarely have together:

- **Data-integrity validation:** declarative data contracts, schema-drift and duplicate detection.
- **Controlled fault injection:** sandbox-only, approved, time-boxed, with a kill-switch.
- **Measured recovery:** RTO and RPO against per-scenario targets, plus post-recovery consistency checks.
- **Evidence:** tamper-evident, signed evidence bundles that anyone can verify offline.

> **Status:** pre-alpha, under active development. See the [build plan](docs/design/README.md).

## Safety boundaries (non-negotiable)

- Fault injection runs **only** against registered SANDBOX or STAGING environments. Production and unknown targets are refused.
- RAI uses **synthetic or explicitly authorized data only**. It never processes real customer PII.
- RAI makes **no autonomous financial decisions** and **no regulatory or compliance claims**.
- No offensive security testing of any kind.

## Quick start (developers)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker, Git.

```bash
make setup     # install dependencies and pre-commit hooks
make check     # lint, type-check and test
uv run rai version
```

## Try it: validate a batch

`make demo` runs the first runnable slice in about 15 seconds:

1. SandboxBank generates two batches of 100,000 synthetic transactions.
2. The defect injector re-sends 100 rows of the first batch inside the second.
3. `rai validate` passes batch 1 (exit code 0) and fails batch 2 with exactly those 100 rows (exit code 1).

To validate your own data, no server is needed:

```bash
uv run rai validate --contract contracts/sandboxbank/transactions_raw.yaml \
    --data path/to/batch.csv --ref accounts=path/to/accounts.csv \
    --ref merchants=path/to/merchants.csv --history .rai/history.sqlite
```

| Exit code | Meaning |
| --- | --- |
| `0` | The batch passes |
| `1` | A blocking rule failed or could not run |
| `2` | The command could not run, for example a bad contract or a missing file |

`--format json` (or `--report FILE`) gives a deterministic JSON report. Contracts are described in [contracts/README.md](contracts/README.md), and connectors (PostgreSQL, Parquet, CSV) in [docs/connectors.md](docs/connectors.md).

`make benchmark` runs the benchmark harness end to end on a small development fixture set; see [benchmarks/README.md](benchmarks/README.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `src/rai/` | The RAI platform: API, core, validation, lineage, recovery, TEVV, evidence, connectors, worker, CLI |
| `chaos_agent/` | The only component allowed to inject faults (separate package, minimal dependencies) |
| `sandboxbank/` | SandboxBank, the synthetic payments system RAI is tested against |
| `web/` | React + TypeScript dashboard |
| `benchmarks/` | Harness and analysis for the pre-registered validation protocol |
| `docs/` | ADRs, decision log, traceability matrix, threat model, build journal, design baseline |

## Traceability

Every change is tied to a requirement in [`docs/traceability.csv`](docs/traceability.csv). Pull-request titles name the requirement, for example `feat(FR-03.3): detect duplicate keys across batches`.

## Synthetic data notice

All data, figures and dashboards in this repository are synthetic examples from SandboxBank. They do not describe any real institution.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions require a DCO sign-off; see [CONTRIBUTING.md](CONTRIBUTING.md).
