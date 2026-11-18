# Resilinet-AI (RAI): Validation & Benchmark Protocol

| | |
| --- | --- |
| **Version** | v0.2 (reconstructed draft), 2026-11-17 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007). **Not frozen and not registered**: the registered protocol must be the author's approved text |
| **Supersedes** | Nothing. The v0.1 original (2026-11-13) has not been found; if it is, it takes precedence over this draft |

> **How this document was made.** Reconstructed on 2026-11-17 by an AI assistant (Claude Code) from the benchmark harness (`benchmarks/`), SandboxBank's injector and fixtures, the decision log and the threat model, because the v0.1 PDF was never exported. Section numbers follow the ones the code cites ("Protocol section 3" and so on). Where the code quotes v0.1 directly, the quotation is kept. A protocol has to be fixed *before* the runs it governs, so this draft must not be used as the registered protocol until the author has reviewed and approved it.

## 1. Purpose and questions

The protocol tests whether RAI finds data defects and measures recovery better than conventional practice, at an acceptable cost, on a system where the truth is known in advance.

- **Q1. Detection:** does RAI detect seeded data defects with higher recall, at comparable or better precision, than a conventional test suite and a profiled Great Expectations suite?
- **Q2. Timeliness:** does it detect them sooner after the batch arrives?
- **Q3. Recovery:** do RAI's measured RTO and RPO agree with ground truth across the controlled fault scenarios?
- **Q4. Cost:** what does RAI add in latency, CPU, memory and authoring effort?

The protocol reports estimates with intervals. It makes no fixed success claim in advance.

## 2. System under test

SandboxBank (Spec section 5), synthetic data only. Pipeline **P1**: the payments feed (`transactions_raw` micro-batches) through `payments-api` into the append-only ledger. Pipeline **P2**: the settlement ETL into the analytics warehouse. Data comes from the seeded generator: **development seeds 0–99,999** for building and tuning; **evaluation seeds 100,000–199,999** for the registered benchmark only. The harness refuses evaluation fixtures unless explicitly allowed for the registered run.

## 3. Comparison arms

| Arm | What it is |
| --- | --- |
| **B1**, conventional | "pytest unit and integration tests written from the pipeline's requirements: required fields, types, a primary-key check per batch". Written from SandboxBank schema v1 only, run once per delivery |
| **B2**, Great Expectations | Suites profiled on 30 clean **development** batches, "then hand-tightened (documented)" from the specification. Every hand edit quotes the specification it comes from |
| **R**, RAI | The SandboxBank contracts through RAI's validation engine, with a persistent cross-batch key history |

- **External review and freeze.** An external engineer reviews both baselines before they are frozen: B1 on day 45, B2 on day 50. Nothing is edited after the freeze. The review packet is `benchmarks/baselines/REVIEW.md`.
- **Effort.** Rule counts and lines of code per arm are reported (`rai-bench effort`).
- **Fair claims.** Every arm may claim a defect class for an alert, derived from what the check is, never from the fixtures.

## 4. Defect catalogue and fixture sets

Twelve classes, with their rates fixed by the protocol:

| Class | Defect | Rates |
| --- | --- | --- |
| D01 | Duplicate key within a batch | 0.01%, 0.1%, 1% |
| D02 | Duplicate key across batches | 0.01%, 0.1%, 1% |
| D03 | Missing required value | 0.01%, 0.1%, 1% |
| D04 | Type error | 0.01%, 0.1%, 1% |
| D05 | Out-of-range value | 0.01%, 0.1%, 1% |
| D06 | Broken reference | 0.01%, 0.1%, 1% |
| D07 | Column renamed | whole batch |
| D08 | Column dropped | whole batch |
| D09 | Column type changed | whole batch |
| D10 | Late-arriving rows | 1%, 5% |
| D11 | Stale feed | whole batch |
| D12 | Join fan-out (duplicated dimension key) | 0.1%, 1% |

- **A fixture set per pipeline** has **180 batches** of 100,000 rows: 12 per defect class, spread evenly over that class's rates, plus **36 clean control batches**.
- **The answer key** names exactly the changed rows (0-based rows of the delivered file), columns and keys, with each batch's window start and onset. It lives in its own folder, which the arms never see.
- Injection rules are recorded in decisions SBX-INJECT and SBX-INJECT-2. For example, the freshness window is 1 h (D10), and D11 re-delivers the previous delivered file exactly.

## 5. Fault types and scenarios

Seven fault types, applied by the chaos agent only in a registered sandbox:

| Type | Mechanism |
| --- | --- |
| latency | Toxiproxy latency toxic |
| timeout | Toxiproxy timeout toxic: calls hang until the client gives up |
| connection_drop | Toxiproxy proxy disabled |
| network_partition | Docker network disconnect |
| container_stop | Docker stop |
| container_restart | Docker restart |
| stale_upstream | Docker pause of the producer: its output stops while it keeps its state |

**Scenarios S01–S20** use these alone or in sequence, each pre-registered with its target, duration, blast radius, recovery probes and RTO/RPO objectives. The repository records these:
- S04, S07, S09: the database becomes unreachable; the ETL must retry until the fault clears.
- S12, S16: the ETL is stopped or restarted mid-load; it must resume with nothing lost or doubled.

The full S01–S20 table is **not recoverable from the repository** and must be restored from v0.1 or re-specified by the author before registration.

## 6. Metrics

Every metric is computed by a script from raw logs, and published with its numerator and denominator:

- **Recall:** seeded defect instances (batch × class) with at least one matching alert, over seeded instances; overall and per class.
- **Precision:** alerts matching a seeded defect, over all alerts. Provisional before adjudication; final after.
- **False alarms on clean controls:** clean batches with any alert, over clean batches.
- **Detection lead time:** first matching alert minus the batch's delivery (onset = batch arrival = window end), in seconds.
- **Validation latency:** P50 and P95 wall seconds per batch (nearest-rank, so every value was observed), CPU seconds and peak memory.
- **Recovery:** RTO and RPO per scenario against its objectives, with the number of runs meeting them over runs attempted. RPO ground truth comes from the seeding log of acknowledged writes (B-30), not from the system under test.

## 7. Statistical analysis

- Paired comparisons on the same batches: **McNemar's test** for detection (per defect instance), **Wilcoxon signed-rank** for lead time and latency, and **bootstrap** confidence intervals for recall and precision.
- The tests are written before the protocol is frozen and run on dry-run data first.
- Sensitivity analyses: by defect class and by rate.

## 8. Alerts, matching, sealing and adjudication

- **Common alert format.** "Each arm writes alerts in one common format: time, batch or injection, location, message." One JSON object per line (`rai-bench-alert/1`): arm, subject (batch or injection), time, location, message and an optional claimed defect class.
- **Matching rule, fixed before any run:** an alert matches a seeded defect if it names the same batch and either the affected column or the defect class. The scorer applies it automatically before human review.
- **Sealing.** The SHA-256 of each answer key (its SEAL) and of the fault log are **committed publicly before any arm runs**. The scorer refuses a key that no longer matches its seal.
- **Blind adjudication.** Arms are replaced by random codes; adjudicators relabel alerts without knowing which arm raised them.
- **Roles.** Reviewer and adjudicator roles are defined by criteria, not by name, and a key custodian holds the answer keys until scoring (decision PROTO).

## 9. Reference environment

Every result file records the environment: OS, CPU count, Python and package versions, the `uv.lock` hash and the git commit. Local paths and user names are never recorded, because results are published. Performance targets (NFR-PERF-1: 100k rows × 30 rules under 10 s) are confirmed on an 8 vCPU reference machine.

## 10. Registration and deviations

The protocol is registered on OSF under embargo before the evaluation runs (decision PROTO), with the answer-key and fault-log seals. Any deviation after registration is recorded with its reason and reported with the results.

## 11. Limitations

- SandboxBank is synthetic; results show behaviour on a controlled system, not on a production bank.
- The author built RAI and the protocol, so there is self-evaluation bias (threat T-06). It is reduced by sealed keys, frozen baselines, external baseline review and blind adjudication, not removed.
- Fault injection is limited to Docker Compose targets (ADR-003).

## Not recoverable from the repository

- The S01–S20 scenario table (section 5).
- v0.1's exact pre-registered hypotheses and decision thresholds, beyond what the code quotes.
