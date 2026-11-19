# Resilinet-AI (RAI): Product & System Requirements Specification

| | |
| --- | --- |
| **Version** | v0.2 (reconstructed draft), 2026-11-17; aligned with `docs/traceability.csv` 2026-11-18 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007) |
| **Supersedes** | Nothing. The v0.1 original (2026-11-13) was written outside this repository and has not been found; if it is, it takes precedence over this draft |

> **How this document was made.** The v0.1 design set was written on 2026-11-13 (build journal, entry 1) but never exported to `docs/design/`. This v0.2 was reconstructed on 2026-11-17 by an AI assistant (Claude Code) from the repository's own records: the ADRs, the decision log, the threat model, the traceability matrix, the build journal, the code and the tests. It is not the original text. Section numbers follow the ones the code cites (for example "Spec section 5"). Where the repository does not record what v0.1 said, this draft says so instead of guessing.

## 1. Purpose and scope

RAI is an open reference framework for the continuous assurance of data pipelines and transaction-support software in financial services. It combines:

- **data-integrity validation**: declarative data contracts checked on every batch, in CI and on demand;
- **lineage with content digests**: which data a test actually read, as SHA-256;
- **controlled fault injection**: chaos scenarios run only in registered sandbox or staging environments;
- **recovery measurement**: RTO, RPO and post-recovery consistency;
- **governed release gates and evidence**: verdicts that block a merge, human approvals, exportable evidence.

**Operating boundary.** RAI runs only against sandbox and staging environments, and never processes real customer data. It does not inject faults into production, does not certify regulatory compliance, and does not make financial decisions. The boundary is enforced in the design (sections 2 and 4), not only stated.

**Primary users.** Engineering and QA teams at smaller financial institutions, fintech providers, and researchers who need repeatable assurance methods but have no dedicated chaos-engineering or TEVV team.

## 2. Design rules

These rules are part of the requirements. A change that breaks one is a defect, whatever else it achieves.

1. **Synthetic or authorised-snapshot data only.** RAI is not built to hold production personal data. Data classification is one of `synthetic` or `authorized_snapshot` (Data Model section 3).
2. **Deny by default.** A fault is injected only when every check passes: registered environment, allow-listed target, approved scenario, declared blast radius, duration within the ceiling. Production cannot be represented (Data Model section 5).
3. **Separation of duties.** Nobody approves their own scenario; the database refuses it as well as the API.
4. **Reproducibility.** Every run pins the exact versions it uses, with digests; seeded data generation is byte-identical on every platform.
5. **Tamper evidence, stated honestly.** Audit and evidence are hash-chained and verifiable. That makes changes detectable, not impossible (NFR-AUD-2).
6. **Tenants name things; operators configure access.** Test definitions name connectors, assets and targets. Paths, addresses and credentials come only from operator configuration and the admin-curated allow-list.

## 3. Functional requirements

Priorities: **M** must (MVP), **S** should, **L** later. Each row's ID, priority and wording are identical to `docs/traceability.csv`; status, backlog items, pull requests, tests and evidence are kept only there, because the matrix is the live record.

| ID | Module | Pri. | Requirement |
| --- | --- | --- | --- |
| FR-01.1 | M01 Tenancy | M | Tenants, users and roles; cross-tenant isolation |
| FR-01.2 | M01 | M | Hash-chained audit entry for every state change |
| FR-01.3 | M01 | L | SSO via OIDC |
| FR-02.1 | M02 Connectors | M | PostgreSQL and CSV/Parquet connectors scoped to one environment |
| FR-02.2 | M02 | S | Redshift connector |
| FR-02.3 | M02 | M | Credentials only as secret references |
| FR-03.1 | M03 Contracts | M | Declarative data contracts (YAML) |
| FR-03.2 | M03 | M | Schema drift detection, breaking vs non-breaking |
| FR-03.3 | M03 | M | Duplicate transaction IDs within and across batches |
| FR-03.4 | M03 | M | Rule severity and thresholds; critical fails the run |
| FR-04.1 | M04 Lineage | M | Lineage edges with content digests |
| FR-04.2 | M04 | L | OpenLineage ingest |
| FR-05.1 | M05 Privacy | S | Synthetic canary privacy sentinel |
| FR-06.1 | M06 Orchestration | M | CLI and GitHub Action; non-zero exit on gate failure |
| FR-06.2 | M06 | S | Idempotent, resumable runs |
| FR-07.1 | M07 Fault injection | M | Fault types (first five families in MVP) |
| FR-07.2 | M07 | M | Preflight: allow-list, not production, approved, blast radius |
| FR-07.3 | M07 | M | Automatic fault removal and kill-switch |
| FR-08.1 | M08 Recovery | M | RTO from onset to validated restoration |
| FR-08.2 | M08 | M | RPO from last committed ledger record |
| FR-08.3 | M08 | M | Post-recovery consistency checks |
| FR-09.1 | M09 TEVV | S | Model evaluation with sample sizes |
| FR-09.2 | M09 | S | Drift detection (PSI/KS) |
| FR-09.3 | M09 | L | Robustness tests |
| FR-10.1 | M10 Findings | M | Findings with severity, owner, fix and retest |
| FR-11.1 | M11 Dashboard | M | Executive dashboard with denominators |
| FR-11.2 | M11 | M | Persistent synthetic-data banner |
| FR-12.1 | M12 Approvals | M | Named reviewer approvals; no self-approval |
| FR-12.2 | M12 | M | Critical finding blocks gate unless waived with expiry |
| FR-13.1 | M13 Evidence | M | Deterministic evidence bundle with SHA-256 manifest |
| FR-13.2 | M13 | S | Signed manifest and `rai verify` |
| FR-14.1 | M14 Dissemination | M | Docs site, quick-start and playbooks |

## 4. Non-functional requirements

| ID | Area | Pri. | Requirement |
| --- | --- | --- | --- |
| NFR-SAF-1 | Safety | M | Deny-by-default fault injection; production fails closed |
| NFR-SAF-2 | Safety | M | Max duration, blast radius, rollback; kill-switch without API |
| NFR-SEC-1 | Security | M | Tenant isolation in API and PostgreSQL RLS |
| NFR-SEC-2 | Security | M | TLS; secrets never persisted |
| NFR-SEC-3 | Security | M | SSDF practices: dependency scanning, SBOM, signed images |
| NFR-SEC-4 | Security | M | Versioned STRIDE threat model |
| NFR-REP-1 | Reproducibility | M | Runs pin versions, digests and seeds |
| NFR-REP-2 | Reproducibility | M | One-command stack on an 8 GB laptop |
| NFR-AUD-1 | Auditability | M | Append-only results and approvals |
| NFR-AUD-2 | Auditability | M | Honest tamper-evidence statements |
| NFR-PERF-1 | Performance | M | 100k-row, 30-rule validation under 10 s |
| NFR-PERF-2 | Performance | M | RAI overhead measured and reported |
| NFR-USE-1 | Usability | M | Every metric shows definition, n and window |
| NFR-USE-2 | Accessibility | M | WCAG 2.2 AA |
| NFR-OPS-1 | Operability | M | Structured logs, traces, health endpoints |
| NFR-LIC-1 | Licensing | M | Apache-2.0 with DCO sign-off |

## 5. The system under test: SandboxBank

RAI is evaluated against **SandboxBank**, a synthetic payments system built for this purpose. It holds synthetic data only: every name and identifier is generated, and nothing is sampled from real people or businesses.

- **Pipeline P1, payments.** `payments-api` (FastAPI) posts transactions into `ledger-db` (PostgreSQL 16). `txn_id` is the idempotency key, and the ledger is append-only.
- **Pipeline P2, settlement.** `etl-job` copies the ledger into an analytics warehouse incrementally and rebuilds daily settlement tables. It is built to resume after a stop or restart without losing or doubling rows.
- **Fraud model.** A gradient-boosted classifier trained on synthetic data, with a published model card and generator (ADR-005). Built: `sandboxbank model`, card in `sandboxbank/MODEL_CARD.md`; RAI's own model evaluation (FR-09.1) is planned.

**Reproducibility rules (NFR-REP-1).**
- Every random stream is seeded by a SHA-256 derivation of the dataset seed.
- Output is CSV and integer draws from the standard library, so the bytes are identical on every platform; a golden-hash test checks this.
- Seed ranges are enforced: development 0–99,999, evaluation 100,000–199,999. Evaluation seeds are never used to develop or tune RAI.
- Each dataset has a SHA-256 manifest.

**Schema v1** (accounts, merchants, `transactions_raw`) is defined in `sandboxbank/README.md` (decision SBX-SCHEMA). v0.1 of this specification gave no column schema.

## 6. Users and roles

| Role | Can |
| --- | --- |
| `admin` | Manage members, environments and the target allow-list; everything an engineer can; review scenarios |
| `engineer` | Create projects, assets, contracts, test cases, plans and scenarios; start runs |
| `reviewer` | Approve or reject scenarios (never their own) |
| `viewer` | Read |
| `auditor` | Read, including the audit chain |

Project API keys (scopes `runs:start`, `runs:read`) let CI start and read runs without a user session.

## 7. Acceptance

Each requirement's acceptance is a named test, recorded in `docs/traceability.csv`. The first end-to-end acceptance, the **first runnable slice**, is in Build Plan section 5. Safety acceptance is the refusal catalogue (`docs/refusals.md`): every way a fault is refused, and the test that proves it.

## 8. Out of scope for the MVP

- Fault injection outside Docker Compose targets (a Kubernetes/Chaos Mesh adapter is a year-2 item; ADR-003).
- Hosted or multi-tenant SaaS operation (ADR-009: self-hosted only).
- Any production data, production environment or regulatory certification.

## 9. Assumptions and constraints

- The host, the Docker daemon and the hardware are trusted (threat model section 1).
- The MVP runs on a single host with private Docker networks; mTLS is an upgrade path.
- Capacity is about 25 hours a week; the pace is gate-driven (Build Plan section 9).

## 10. Traceability

`docs/traceability.csv` maps every requirement to its backlog items, pull requests, tests and evidence. CI checks that every pull-request title names a known requirement ID.

## 11. Decisions

D1–D9 were accepted as ADR-001 to ADR-009 on 2026-11-13; see `docs/adr/` and Architecture section 10.

| ID | Decision |
| --- | --- |
| D1 | Python 3.12 and FastAPI for all backend components (ADR-001) |
| D2 | Own rule engine on Polars, with import of Great Expectations suites (ADR-002) |
| D3 | Toxiproxy plus the Docker SDK for fault injection; Kubernetes later (ADR-003) |
| D4 | React and TypeScript UI; PostgreSQL as the job queue, no message broker (ADR-004) |
| D5 | SandboxBank fraud model: gradient-boosted classifier on synthetic data (ADR-005) |
| D6 | Apache-2.0, public repository from day 1, DCO sign-off (ADR-006) |
| D7 | Trevor Kauyu authors requirements, methods and evaluation; helpers work from tickets (ADR-007) |
| D8 | Pilot outreach to 3–5 U.S. credit unions, a fintech and a university lab after the MVP (ADR-008) |
| D9 | Self-hosted only for the MVP; public read-only demo later (ADR-009) |
| D10 | Midnight (dark) default UI theme; Clarity, Evergreen and High contrast selectable; all meet WCAG 2.2 AA |

## Not recoverable from the repository

- The v0.1 wording of the problem statement, personas and success criteria.
- Any requirement that v0.1 listed but the traceability matrix does not.
