# Resilinet-AI (RAI): Architecture & Design

| | |
| --- | --- |
| **Version** | v0.3 (draft), 2026-11-20; v0.2 reconstructed 2026-11-17 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007) |
| **Baseline** | This document (decision DESIGN-V03). The 2026-11-13 v0.1 drafts, found on 2026-11-20, are not adopted; v0.3 takes from them the risk register (section 11) |

> **How this document was made.** Reconstructed on 2026-11-17 by an AI assistant (Claude Code) from the repository's records (ADR-001 to ADR-010, decision log, threat model v1, code and tests), because the v0.1 PDF was never exported. Section numbers follow the ones the code cites ("Architecture section 5" and so on). It describes the system as built on 2026-11-17, marking planned parts as planned.

## 1. Context

RAI tests a system it does not own: here SandboxBank, a synthetic payments system (Spec section 5). The architecture has two jobs that pull against each other: it has to be able to break the system under test on purpose, and it must not be able to break anything else. Most of the design below exists to keep those apart.

## 2. Components, processes and planes

**Modular monolith, four processes, three networks (ADR-010).** One Python codebase (`src/rai`) with module boundaries enforced by import-linter: `rai.core` must not import the API, CLI or worker, and the engines (validation, recovery, TEVV, lineage) stay independent of one another.

| Process | Network | Role |
| --- | --- | --- |
| `rai-api` | control | FastAPI. Authentication, tenancy, definitions, runs, approvals. Never touches the system under test |
| `rai-worker` | control (+ read-only probes into the sandbox) | Claims queued runs, executes them from their pins, records observations and lineage |
| `rai-chaos-agent` | bridge | The only component that can inject a fault. Deliberately minimal: the standard library plus `cryptography` |
| `rai-web` | control | React/TypeScript UI (ADR-004), built into the Caddy image and served from the API's origin. Sign-in and Runs are built; the other screens are marked planned (`web/README.md`) |

Added during the build: `rai-chaos-watchdog` (B-25), an independent process that reverts overdue faults when the agent is dead or hung; and `rai-observer` (B-44, `observer` profile), which samples the payments API and the ledger every 250 ms on its own clock to give Protocol H3 its ground-truth RTO. It has no `rai.sandbox` label, so no fault can target it.

| Network | Holds | Why separate |
| --- | --- | --- |
| `control_net` | API, worker, RAI database, evidence store | Tenants' definitions and results; no fault ever originates here except through the agent |
| `bridge_net` | Chaos agent, Docker socket proxy | The single path from "decide" to "do" |
| `sandbox_net` | payments-api, ledger-db, etl-job, analytics-db, Toxiproxy | Synthetic data only; no route back to the control network |

```
 Client (untrusted)                control_net                       bridge_net            sandbox_net
 ------------------         ---------------------------        -----------------     ----------------------
 browser --HTTPS--> rai-web --> rai-api --> PostgreSQL (rai)                          payments-api -> ledger-db
 CLI / Action --API key-------> rai-api        ^                                      etl-job -> analytics-db
                                rai-worker ----+--signed request--> rai-chaos-agent --> Toxiproxy
                                     |                                   |            (proxies in front of DBs)
                                     |                              socket-proxy --> Docker (allowed verbs only)
                                     +--------- read-only probes and ledger reads ---> payments-api, ledger
```

Trust boundaries TB1–TB7 are in `docs/threat-model.md` section 2.

## 3. Chaos safety

The chaos agent denies by default, enforces a maximum fault duration, and owns the kill-switch. A fault must pass **two independent preflights**:

1. **The worker**, against the registry: the run has an active environment; every blast-radius target is on that environment's allow-list with the right kind; the duration is within the environment's ceiling; the scenario's latest review is an approval that has not expired (B-43). Only then is the request signed (Ed25519), with the approval's expiry in it.
2. **The agent**, on its own: the signature is from a trusted worker key and is fresh (at most 120 s); the request is for the agent's environment; the duration is within the agent's own ceiling; the target is in the declared radius, and every fault of a scenario keeps that scenario's radius, including faults already reverted; at most N faults are open; a container target is in the agent's own Compose project and carries `rai.sandbox=true` and its registered label; a scenario fault's approval has not expired by the agent's clock. A repeated run and step is a replay (409).

**Removal.** Every fault is written to a local state file before it acts. The agent reverts expired faults, and reverts anything left active when it restarts. The independent watchdog reverts any fault past its duration plus a grace period. `kill --all` works from the state file without the API or database (NFR-SAF-2). Reverts are idempotent.

**Docker access** goes through a socket proxy that allows only the verbs the agent needs (container stop, start, restart, pause, unpause; network connect and disconnect) and only from the agent and the watchdog.

Every refusal and the test proving it: `docs/refusals.md`.

## 4. Time model and recovery definitions

- **Onset (t0):** when the fault took effect, recorded by the agent.
- **Revert:** when the fault was removed, recorded by the agent.
- **Validated restoration:** the first of `stable_checks` consecutive rounds in which every probe answers healthy, after the last revert. A recovery that flaps counts only once it holds.
- **RTO** = validated restoration − onset (FR-08.1).
- **RPO** (FR-08.2) is measured on the ledger, which is append-only: 0 s if no committed row was lost, otherwise onset − the surviving head's commit time. Ground truth comes from a seeding log of acknowledged writes kept outside the system under test (B-30).
- **Consistency** (FR-08.3): after recovery, a target is reconciled against its source: missing, duplicated, unexpected and mismatched keys.

## 5. Execution: runs, queue and worker

- **A run pins everything at queue time:** plan, test-case and contract versions with SHA-256, the target's declared upstream graph, the environment, and for chaos the scenario version and the approval it runs under. The worker executes from the pins only, so a run can always be replayed.
- **States:** `queued → preflight → running → evaluating → passed | failed`, plus `refused` (preflight found something this worker cannot run), `error` (the platform failed three times) and `aborted` (reserved for the kill-switch). A database trigger enforces the machine. Finished runs never change.
- **The queue is PostgreSQL** (ADR-004): `SELECT ... FOR UPDATE SKIP LOCKED` with a lease of 5 minutes, renewed between steps; a retry cap of 3.
- **Steps are atomic and idempotent (FR-06.2):** a step's observations, its step row and its lineage commit in one transaction; a recorded step is never repeated, so a worker killed mid-run leaves nothing half-written.
- **Offline mode:** `rai validate` runs a contract against a batch with no server, for local use and CI.

## 6. Data access

Workers read data only through connectors the operator configures (`RAI_WORKER_CONNECTORS`): CSV and Parquet confined to a root folder, PostgreSQL in a read-only session. Credentials are secret references (`env:`, `file:`), never literals, and are scrubbed from errors. Each connector belongs to one environment.

## 7. Security

- **Users:** Argon2id passwords; a 15-minute HS256 access JWT naming only the user; an opaque refresh token, rotated on use, stored as SHA-256.
- **Tenancy:** row-level security on every tenant table, keyed on a transaction-local `app.tenant_id`. The API connects as a login role that only has `rai_app` (no BYPASSRLS) and refuses to start as the schema owner.
- **CI:** project API keys `rai_<prefix>_<secret>`, only the SHA-256 stored, with scopes, expiry and per-key rate limits.
- **Transport:** HTTPS only at the edge: Caddy terminates TLS (its local CA, or ACME on a public name) and is the only published port; the API sits on an internal network shared only with Caddy and trusts forwarded addresses only from it (B-36, `docs/deployment.md`). Inside the host, private Docker networks; mTLS is the upgrade path.
- The full STRIDE register (36 threats) is `docs/threat-model.md`.

## 8. Evidence and audit

- **Audit chain:** every state change appends a hash-chained entry computed in the database, under a per-tenant advisory lock; a trigger refuses UPDATE, DELETE and TRUNCATE even from the owner. An independent Python verifier recomputes the chain (`docs/audit-chain.md`).
- **Append-only results:** observations, run steps, lineage edges and scenario approvals are insert-only.
- **Evidence bundle** (FR-13): a deterministic export of a finished run with a SHA-256 manifest and an audit-chain anchor, the run's last audit entry (B-34), and an Ed25519 signature over the manifest, checked offline by `rai verify` against a pinned public key (B-35).

## 9. Deployment

One Docker Compose file (`compose.yaml`) with profiles: `core` (the RAI database, the API and Caddy; B-36) and `sandbox` (SandboxBank). Toxiproxy, the socket proxy, the chaos agent and the watchdog join for chaos runs. The whole stack fits an 8 GB laptop (NFR-REP-2). Images run as non-root users with no privileged containers. Self-hosted only for the MVP (ADR-009).

## 10. Decisions

| ADR | Decision |
| --- | --- |
| ADR-001 | Python 3.12 and FastAPI for all backend components; mypy strict |
| ADR-002 | Own rule engine on Polars, with import of Great Expectations suites |
| ADR-003 | Toxiproxy plus the Docker SDK for fault injection; Kubernetes later |
| ADR-004 | React and TypeScript UI; PostgreSQL as the job queue, no message broker |
| ADR-005 | SandboxBank fraud model: gradient-boosted classifier on synthetic data |
| ADR-006 | Apache-2.0, public GitHub repository from day 1, DCO sign-off |
| ADR-007 | Trevor Kauyu authors requirements, methods and evaluation; helpers work from tickets |
| ADR-008 | Pilot outreach to 3–5 U.S. credit unions, a fintech and a university lab after the MVP |
| ADR-009 | Self-hosted only for the MVP; public read-only demo later |
| ADR-010 | Modular monolith with four processes in three planes |

Later decisions are in `docs/decision-log.md`.

## 11. Risks

The biggest technical risk is measurement credibility: if someone else cannot reproduce the RTO and RPO numbers, the rest of the evidence weakens.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| RTO timing noise on a laptop | Results unstable across machines | Monotonic clocks, 1 s probes, repeated runs, spread reported rather than single values; an independent observer measures the same injections (B-44) |
| Health checks pass while data is damaged | RTO looks good on a broken ledger | Post-recovery consistency checks (FR-08.3) reported beside RTO; Protocol H4 measures what health-only monitoring misses |
| SandboxBank too simple to convince | Reviewers discount the results | Built on published core-banking patterns (idempotent API, append-only ledger, resumable ETL); reviewer critique invited |
| Docker socket access is powerful | A fault escapes the sandbox | Socket proxy with allow-listed verbs; agent isolated on `bridge_net`; two preflights; watchdog; threat model |
| A stale approval runs a scenario long after review | The reviewer's judgement no longer applies | Approvals expire (B-43) |
| Runs not reproducible because images moved | Replication fails | Image digests pinned in every run (B-45) |
| One-person scope | The day-90 release slips | Gate-driven pace; MoSCoW cuts; capacity levers (Build Plan section 8) |
