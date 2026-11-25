# Resilinet-AI (RAI): Build Plan

| | |
| --- | --- |
| **Version** | v0.3 (draft), 2026-11-20; v0.2 reconstructed 2026-11-17 and aligned with `docs/traceability.csv` 2026-11-18 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007) |
| **Baseline** | This document (decision DESIGN-V03). The 2026-11-13 v0.1 drafts, found on 2026-11-20, are not adopted; v0.3 takes from them B-41, the capacity levers, the test targets and the gate dates, and adds B-42 to B-46 |

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
| Later | B-16 onwards | Gated by the dates in section 9 | In progress |

## 4. Backlog

| Item | Scope | Requirements | Status |
| --- | --- | --- | --- |
| B-01 | Repository bootstrap: CI, licence, DCO sign-off | NFR-SEC-3, NFR-LIC-1 | Done |
| B-02 | Branch protection; SandboxBank generator: seed ranges, SHA-256 manifest, golden hash | NFR-REP-1 | Done |
| B-03 | payments-api and ledger-db: idempotent posting, append-only ledger | NFR-REP-2 | Done |
| B-04 | etl-job and analytics-db (P2) | NFR-REP-2 | Done |
| B-05 | RAI core schema: tenants, RLS, hash-chained audit | FR-01.1, FR-01.2, NFR-SEC-1, NFR-AUD-2 | Done |
| B-06 | Threat model v1 (STRIDE) | NFR-SEC-3, NFR-SEC-4 | Done |
| B-07 | Defect injector for the 12 classes; sealed fixture sets | NFR-REP-1 | Done |
| B-08 | Contract YAML parser and SandboxBank contracts | FR-03.1, FR-03.4 | Done |
| B-09 | Polars rule engine | FR-03.1, NFR-PERF-1 | Done |
| B-10 | Duplicates within and across batches | FR-03.3, NFR-PERF-1 | Done |
| B-11 | Schema drift detection | FR-03.2 | Done |
| B-12 | Batch and run verdicts | FR-03.4 | Done |
| B-13 | CSV, Parquet and PostgreSQL connectors with secret references | FR-02.1, FR-02.3 | Done |
| B-14 | `rai validate` and the first runnable slice | FR-06.1 | Done |
| B-15 | Benchmark harness (`rai-bench`) | NFR-REP-1, NFR-PERF-1, NFR-PERF-2 | Done |
| B-16 | API sign-in, tenants, members, RBAC | FR-01.1, NFR-SEC-1 | Done |
| B-17 | Projects, assets, contracts, test cases, plans | FR-01.2, FR-03.1, FR-03.4, FR-04.1, FR-06.1, FR-11.2 | Done |
| B-18 | Runs, PostgreSQL job queue, worker | FR-06.1, FR-06.2, NFR-REP-1 | Done |
| B-19 | Lineage with SHA-256 digests | FR-04.1, NFR-REP-1 | Done |
| B-20 | Project API keys, `rai run`, GitHub Action gate | FR-06.1, NFR-SEC-1 | Done |
| B-21 | Baselines B1 and B2 drafted for review | NFR-REP-1, NFR-PERF-2 | Done |
| B-22 | Environments and the target allow-list | FR-07.2, NFR-SAF-1 | Done |
| B-23 | Chaos agent: seven fault types | FR-07.1, FR-07.3, NFR-SAF-1 | Done |
| B-24 | Signed fault requests and the second preflight | FR-07.1, FR-07.2, NFR-SAF-1 | Done |
| B-25 | Independent watchdog and kill-switch drill | FR-07.3, NFR-SAF-1 | Done |
| B-26 | Chaos scenarios with separated review; B-26b RTO/RPO measurement | FR-07.1, FR-07.2, FR-08.1, FR-08.2 | Done |
| B-27 | Scenario-level blast radius; refusal suite | FR-07.2, NFR-SAF-1 | Done |
| — | Post-recovery consistency checks (FR-08.3; no backlog number recorded) | FR-08.3 | Done |
| — | Executive dashboard: the summary API and the UI's Dashboard screen (FR-11.1, NFR-USE-1; no backlog number recorded) | FR-11.1, NFR-USE-1 | Done |
| — | Structured logs, W3C trace context and health endpoints (NFR-OPS-1; no backlog number recorded) | NFR-OPS-1 | Done |
| — | WCAG 2.2 AA checks for the UI: axe-core on every screen and theme, theme contrast, and the fixes they found (NFR-USE-2; no backlog number recorded) | NFR-USE-2 | Done |
| — | The DCO checked in CI and the Apache-2.0 declarations tested (NFR-LIC-1; the licence and sign-off themselves date from B-01) | NFR-LIC-1 | Done |
| — | Docs site, quick-start and playbooks (FR-14.1; no backlog number recorded) | FR-14.1 | Done |
| — | Drift checks: PSI and KS between a pinned reference and the current data (FR-09.2; no backlog number recorded) | FR-09.2 | Done |
| — | SandboxBank fraud model and model card (ADR-005), the model FR-09.1 evaluates | FR-09.1 | Done |
| — | Model evaluation: the `tevv_metric` test type, every metric with its counts and confidence interval (FR-09.1; no backlog number recorded) | FR-09.1 | Done |
| — | Single sign-on through OpenID Connect: authorization code with PKCE, ID token verified against the provider's JWKS, invited members only, linked by verified email then by subject; checked live against Keycloak 26 (FR-01.3; no backlog number recorded) | FR-01.3 | Done |
| — | OpenLineage ingest: an orchestrator posts run events with a `lineage:write` key; RAI keeps their core (never facets), maps datasets to assets and shows external jobs in a project's graph (FR-04.2; no backlog number recorded) | FR-04.2 | Done |
| — | Robustness tests: the `robustness_check` test type perturbs a feature table itself (noise, missing, unknown category; seeded) and has each variant scored by SandboxBank's model-api through an allow-listed `score_url` (FR-09.3; no backlog number recorded) | FR-09.3 | Done |
| — | Safety limits mapped to their evidence: maximum duration, blast radius, rollback and the kill switch, built in B-23, B-25 and B-27 | NFR-SAF-2 | Done |
| — | Privacy sentinel, part 1: SandboxBank's canary customers and their answer key | FR-05.1 | Done |
| — | Privacy sentinel, part 2: the `privacy_canary` test type searches data assets, every hit masked | FR-05.1 | Done |
| — | Privacy sentinel, part 3: `rai canary scan` sweeps logs and evidence bundles; CI sweeps RAI's logs, the gate runs' bundles and the sandbox services' logs | FR-05.1 | Done |
| — | Redshift connector: read-only sessions set after connecting, a server-side cursor instead of `COPY`, TLS required. Tested on PostgreSQL 16; a run against a live cluster is still owed | FR-02.2 | In progress |
| B-28 | Benchmark sealing for the chaos benchmark (threat T-06, with B-07). The sealing mechanism is done (`rai-bench chaos-plan`, `chaos-seal`). P3 joins the study (decided 2026-11-21): the P3 feature pipeline is built and S10, S13 and S18 are specified. The P2 and P3 defect fixtures, their RAI contracts and arm R are done, with all twelve classes adapted (decided 2026-11-24). The P2 and P3 baselines (B1, B2) are written and smoke-run, in the same review packet as P1's. The author approved the review on 2026-11-25: all objectives, with RPO 0 everywhere, and freshness probes for P2 and P3. The probes are built. Still to come: the seal | NFR-REP-1 | In progress |
| B-29 | Append-only recovery measurements (threat T-03) | FR-08.1, FR-08.2, NFR-AUD-1 | Done |
| B-30 | RPO ground truth from a seeding log (threat T-07) | FR-08.2 | Done |
| B-31, B-32 | Not used (section 9) | — | — |
| B-33 | Waivers with expiry on blocking findings, and the release gate (FR-12.2) | FR-12.1, FR-12.2 | Done |
| B-34 | Evidence bundle with SHA-256 manifest and audit-chain head (FR-13.1) | FR-13.1 | Done |
| B-35 | Signed manifest and `rai verify` (FR-13.2) | FR-13.2 | Done |
| B-36 | TLS through Caddy (NFR-SEC-2) | NFR-SEC-2 | Done |
| B-37 | Persistent synthetic-data banner in the UI (FR-11.2), with the UI's first slice: sign-in, the shell and Runs | FR-11.2, FR-01.1 | Done |
| B-38, B-39 | Not used (section 9) | — | — |
| B-40 | Image hardening (SSDF: SBOM, signed images; NFR-SEC-3) | NFR-SEC-3 | Done |
| B-41 | RAI v1.0 release: tag, signed images, release notes, and a reproduction on a clean 8 GB laptop. The machinery is done (workflow "Release", `scripts/release.py`, `make reproduce`, draft notes `docs/releases/v1.0.0.md`); the tag and the laptop reproduction wait for day 90, after the protocol is registered | NFR-REP-2 | In progress |
| B-42 | Release decisions: a named reviewer approves or rejects a labelled release, recorded with the gate's state; approval refused while the gate is blocked and from anyone whose waiver or finding it relies on | FR-12.1, FR-12.2 | Done |
| B-43 | Scenario approvals expire (30 days by default, at most 90); an expired approval is refused when a run is queued, by the worker before each fault and by the agent | FR-07.2, NFR-SAF-1 | Done |
| B-44 | Independent recovery observer: ground-truth RTO for Protocol H3, outside every arm (`sandboxbank observe`, `rai-bench h3`) | FR-08.1 | Done |
| B-45 | Human-readable run report with the limitations statement in every bundle (`report.md`, `bundle.json` `limitations`); container image digests pinned in every run and enforced by the worker and the agent | FR-13.1, NFR-REP-1, NFR-AUD-2 | Done |
| B-46 | End-to-end tests for journeys J1–J5 (Spec section 13), nightly (`tests/e2e/test_journeys.py`, workflow "Journeys", `make journeys`) | NFR-REP-2 | Done |

The Requirements column is the `backlog` column of `docs/traceability.csv`, read in reverse; for planned items it is the requirement named in the scope, and "—" where the matrix records none. The matrix is the live record: if the two disagree, the matrix wins and this table is corrected.

Requirements the matrix maps to no backlog item yet: FR-02.2 (built, not yet run on a live cluster), FR-05.1, FR-08.3, FR-10.1, FR-11.1, NFR-USE-1, NFR-OPS-1, NFR-USE-2, FR-14.1, and FR-09.2, FR-09.1, FR-09.3, FR-04.2 and FR-01.3 (built, number not recorded).

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

Capacity is the main risk (journal entry 3). When a gate is at risk, use these levers in order, and record the choice in the decision log:

1. **AI-assisted coding** for boilerplate (endpoints, migrations, tests, UI components), with the author directing the design and reviewing every change.
2. **Cut UI scope** to the dashboard, runs and run detail; approvals, evidence and environments work through the API and CLI in v1.0.
3. **Add hours** in the heaviest stretch.
4. **Move the v1.0 release** by up to 15 days and shift the benchmark with it. This is the last resort, because the protocol's order (section 9) must still hold.

## 8a. Test targets

| Layer | Target |
| --- | --- |
| Unit | Line coverage reported in CI; branch coverage of the recovery arithmetic and both preflights kept at 100% |
| Property-based | Hypothesis: rules never miss a seeded defect; manifests stable under file order; the run state machine never leaves a terminal state |
| Safety | The refusal catalogue (`docs/refusals.md`) 100% green on every change to the chaos path |
| Measurement validity | RTO within ± 2 s of the independent observer on five scenarios (B-44) |
| End-to-end | One test per journey J1–J5 (B-46): against a live API, worker, SandboxBank and chaos agent, every night |
| Accessibility | axe-core on every screen in all four themes, no serious or critical issue |

## 9. Schedule and working assumptions

- **Day 1 = 2026-11-16**; about 25 hours a week (decision BUILD).
- AI may draft; the author reviews and owns every change.
- Dates are latest-by dates (section 1). Fixed points recorded so far: baseline B1 frozen on day 45 and B2 on day 50, after external review.
- Pilot outreach (3–5 U.S. credit unions, a fintech and a university lab) starts after the v1.0 release (ADR-008).

**Gates and latest-by dates** (day 1 = 2026-11-16):

| Day | Date | Gate |
| --- | --- | --- |
| 45 | 2026-12-30 | Baseline B1 frozen after external review |
| 50 | 2027-01-04 | Baseline B2 frozen |
| 60 | 2027-01-14 | Protocol approved, frozen and registered on OSF; the S01–S20 registry sealed (B-28) |
| 75 | 2027-01-29 | RTO within ± 2 s of the independent observer on five scenarios (B-44) |
| 90 | 2027-02-13 | RAI v1.0 tagged and reproduced on a clean laptop (B-41); frozen before any benchmark run |

The protocol's order never changes, whatever the pace: baselines frozen, then the protocol registered, then RAI v1.0 frozen, then the benchmark.

## Not recorded

- B-31, B-32, B-38 and B-39 have no recorded scope; their work was done without those numbers (section 4).
