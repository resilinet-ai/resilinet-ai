# Resilinet-AI (RAI): Build Plan

| | |
| --- | --- |
| **Version** | v0.2 (reconstructed draft), 2026-11-17 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007) |
| **Supersedes** | Nothing. The v0.1 original (2026-11-13) has not been found; if it is, it takes precedence over this draft |

> **How this document was made.** Reconstructed on 2026-11-17 by an AI assistant (Claude Code) from the build journal, the decision log, the threat model's backlog references, the traceability matrix and the git history, because the v0.1 PDF was never exported. Section numbers follow the ones the repository cites (section 5 for the first runnable slice, section 8 for capacity levers, section 9 for the schedule). **The backlog in section 4 is incomplete:** items the repository never mentions are marked as gaps, and the order after B-27 was chosen during the build, not taken from v0.1.

## 1. Approach

- **Gate-driven pace.** A sprint closes as soon as its gate passes; the dates are latest-by dates, not fixed calendar sprints (decision PACE).
- **Thin vertical slices.** The first goal is one runnable path end to end (section 5); every item after that extends a working system.
- **Evidence as a by-product.** Every item leaves its trail as it goes: signed commits, a pull request whose title names its requirement IDs, tests named in the traceability matrix, a decision-log entry and a journal entry.

## 2. Roles

- **Trevor Kauyu** authors the requirements, methods and evaluation, and reviews and owns every change (ADR-007).
- **Helpers**, human or AI, work from tickets. AI may draft code and documents; AI assistance is disclosed in pull requests, and commits carry a `Co-Authored-By` trailer.
- **External reviewers** review the baselines before they freeze (Protocol section 3), and later RAI itself.

## 3. Sprints and gates

| Sprint | Items | Gate (as recorded) | Status |
| --- | --- | --- | --- |
| 1 | B-01 to B-07: repository, SandboxBank generator, P1 and P2, core schema, threat model, defect injector | SandboxBank generates, posts and settles; sealed fixture sets exist | Done (journal entries 6–11) |
| 2 | B-08 to B-15: contracts, rule engine, duplicates, drift, verdicts, connectors, `rai validate`, benchmark harness | The first runnable slice passes (section 5); `make benchmark` runs | Done (journal entries 12–19) |
| Later | B-16 onwards | Not recoverable: v0.1's later sprint boundaries and gates are not recorded in the repository | In progress |

## 4. Backlog

| Item | Scope | Status |
| --- | --- | --- |
| B-01 | Repository bootstrap: CI, licence, DCO sign-off | Done |
| B-02 | Branch protection; SandboxBank generator: seed ranges, SHA-256 manifest, golden hash | Done |
| B-03 | payments-api and ledger-db: idempotent posting, append-only ledger | Done |
| B-04 | etl-job and analytics-db (P2) | Done |
| B-05 | RAI core schema: tenants, RLS, hash-chained audit | Done |
| B-06 | Threat model v1 (STRIDE) | Done |
| B-07 | Defect injector for the 12 classes; sealed fixture sets | Done |
| B-08 | Contract YAML parser and SandboxBank contracts | Done |
| B-09 | Polars rule engine | Done |
| B-10 | Duplicates within and across batches | Done |
| B-11 | Schema drift detection | Done |
| B-12 | Batch and run verdicts | Done |
| B-13 | CSV, Parquet and PostgreSQL connectors with secret references | Done |
| B-14 | `rai validate` and the first runnable slice | Done |
| B-15 | Benchmark harness (`rai-bench`) | Done |
| B-16 | API sign-in, tenants, members, RBAC | Done |
| B-17 | Projects, assets, contracts, test cases, plans | Done |
| B-18 | Runs, PostgreSQL job queue, worker | Done |
| B-19 | Lineage with SHA-256 digests | Done |
| B-20 | Project API keys, `rai run`, GitHub Action gate | Done |
| B-21 | Baselines B1 and B2 drafted for review | Done |
| B-22 | Environments and the target allow-list | Done |
| B-23 | Chaos agent: seven fault types | Done |
| B-24 | Signed fault requests and the second preflight | Done |
| B-25 | Independent watchdog and kill-switch drill | Done |
| B-26 | Chaos scenarios with separated review; B-26b RTO/RPO measurement | Done |
| B-27 | Scenario-level blast radius; refusal suite | Done |
| — | Post-recovery consistency checks (FR-08.3; no backlog number recorded) | Done |
| B-28 | Benchmark sealing for the chaos benchmark (threat T-06, with B-07) | Planned |
| B-29 | Append-only recovery measurements (threat T-03) | Planned |
| B-30 | RPO ground truth from a seeding log (threat T-07) | Done |
| B-31, B-32 | Not recoverable | — |
| B-33 | Waivers with expiry on blocking findings (FR-12.2) | Planned |
| B-34 | Evidence bundle with SHA-256 manifest and audit-chain head (FR-13.1) | Planned |
| B-35 | Signed manifest and `rai verify` (FR-13.2) | Planned |
| B-36 | TLS through Caddy (NFR-SEC-2) | Planned |
| B-37 | Persistent synthetic-data banner in the UI (FR-11.2) | Planned |
| B-38, B-39 | Not recoverable | — |
| B-40 | Image hardening (SSDF: SBOM, signed images; NFR-SEC-3) | Planned |

Requirements with no backlog number yet: FR-09 (TEVV), FR-10 (findings), FR-11.1 (dashboard), FR-12.1 (named approvals for runs), FR-14 (docs and playbooks), NFR-USE, NFR-OPS. The live status of every requirement is `docs/traceability.csv`.

## 5. First runnable slice: acceptance

One command (`make demo`) demonstrates the core loop on synthetic data:

1. Generate one clean batch of 100,000 rows, then a second batch that re-sends 0.1% of the first batch's rows (D02: 100 of 100,000).
2. `rai validate` the clean batch with the SandboxBank contract: **exit 0**.
3. Validate the second batch against the same key history: **exit 1**, naming **exactly the 100 seeded rows**.
4. Run it twice: the JSON reports are **byte-identical**.

Accepted by `tests/unit/test_cli_validate.py::test_first_runnable_slice` (B-14).

## 6. Definition of done

- Tests for the change, passing in CI: lint (ruff), format, types (mypy strict), import contracts, unit tests, and integration tests against real PostgreSQL.
- A pull request whose title names its requirement IDs (checked by CI), with DCO sign-off and signed commits.
- `docs/traceability.csv` updated; a decision-log entry for every non-obvious choice; a journal entry.
- Threat model updated when a threat's status changes.
- Merged by the author on GitHub.

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| Capacity: one author at about 25 h a week | Gate-driven pace; thin slices; the levers in section 8 |
| Self-evaluation bias in the benchmark (T-06) | Sealed answer keys, frozen baselines, external review, blind adjudication |
| A fault escapes the sandbox | Two preflights, signed requests, watchdog, kill-switch, refusal catalogue (`docs/refusals.md`) |
| Authorship questioned (ADR-007) | Decision log, tickets, signed commits; AI assistance disclosed |
| Supply chain | Actions pinned by SHA, Dependabot with a 14-day cooldown, uv resolves nothing younger than 14 days |

## 8. Capacity levers

Journal entry 3 records that capacity is the main risk, and that the levers are listed in this section of v0.1. **The list itself is not recoverable from the repository.** It must be restored from v0.1 or written by the author.

## 9. Schedule and working assumptions

- **Day 1 = 2026-11-16**; about 25 hours a week (decision BUILD).
- AI may draft; the author reviews and owns every change.
- Dates are latest-by dates (section 1). Fixed points recorded so far: baseline B1 frozen on day 45 and B2 on day 50, after external review.
- Pilot outreach (3–5 U.S. credit unions, a fintech and a university lab) starts after the v1.0 release (ADR-008).

## Not recoverable from the repository

- Sprint boundaries and gates after Sprint 2; B-31, B-32, B-38 and B-39.
- The capacity levers (section 8) and v0.1's milestone dates beyond day 1, day 45 and day 50.
