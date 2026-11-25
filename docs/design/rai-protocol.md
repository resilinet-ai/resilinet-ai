# Resilinet-AI (RAI): Validation & Benchmark Protocol

| | |
| --- | --- |
| **Version** | v0.3 (draft), 2026-11-20 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007). **Not frozen and not registered**: the registered protocol must be the author's approved text |
| **Baseline** | This document (decision DESIGN-V03). The 2026-11-13 v0.1 drafts were found on 2026-11-20; v0.3 adopts from them only what the build lacked: the hypotheses and decision rules (sections 1 and 7), the S01–S20 table (section 5), the replication criterion (section 10) and the threats to validity (section 11) |

> **How this document was made.** v0.2 was reconstructed on 2026-11-17 by an AI assistant (Claude Code) from the benchmark harness (`benchmarks/`), SandboxBank's injector and fixtures, the decision log and the threat model. v0.3 (2026-11-20) adds the parts above, adapted to SandboxBank as built. Section numbers follow the ones the code cites ("Protocol section 3" and so on). A protocol has to be fixed *before* the runs it governs, so this draft must not be used as the registered protocol until the author has reviewed and approved it.

## 1. Purpose and questions

The protocol tests whether RAI finds data defects and measures recovery better than conventional practice, at an acceptable cost, on a system where the truth is known in advance.

- **Q1. Detection:** does RAI detect seeded data defects with higher recall, at comparable or better precision, than a conventional test suite and a profiled Great Expectations suite?
- **Q2. Timeliness:** does it detect them sooner after the batch arrives?
- **Q3. Recovery:** do RAI's measured RTO and RPO agree with ground truth across the controlled fault scenarios?
- **Q4. Cost:** what does RAI add in latency, CPU, memory and authoring effort?

The protocol reports estimates with intervals. It makes no fixed success claim in advance, but each question has a hypothesis and a decision rule fixed now, so the result cannot be reinterpreted afterwards.

| ID | Hypothesis (H0: no difference) | Primary metric | Type |
| --- | --- | --- | --- |
| H1 (Q1) | RAI's recall is higher than the better baseline's, and its precision is not lower by more than 0.05 | Recall and precision per defect instance | Primary |
| H2 (Q2) | RAI's median detection lead time is shorter than the better baseline's | First matching alert minus delivery, seconds | Primary |
| H3 (Q3) | RAI's RTO is within ± 2 s of the ground-truth observer in at least 95% of injections, and its RPO matches the seeding log exactly | Absolute error against ground truth | Primary |
| H4 | RAI flags lost, duplicated or unexpected rows after recovery that health-check-only monitoring (B2) misses | Post-recovery damage detected, per injection | Secondary |
| H5 (Q4) | Validation P95 is at most 10 s per 100k-row batch; CPU and memory are reported | P50/P95 latency, CPU seconds, peak memory | Secondary, descriptive |
| H6 | Drift checks flag seeded distribution shifts with at most 5% false alarms on unshifted windows | Detection delay, false-alarm rate | Secondary |

Only H1–H3 are confirmatory and used for a headline claim. RAI is not expected to win everywhere: a baseline may match it on simple defects such as nulls, and results are broken down by defect class so both wins and losses are visible.

## 2. System under test

SandboxBank (Spec section 5), synthetic data only. Pipeline **P1**: the payments feed (`transactions_raw` micro-batches) through `payments-api` into the append-only ledger. Pipeline **P2**: the settlement ETL into the analytics warehouse. Pipeline **P3**: the fraud feature pipeline (`feature-job`). It reads P2's warehouse, has each transaction scored by the fraud model's service (`model-api`), and writes features and scores to `features.fraud_features` (decided 2026-11-21: P3 joins the study). Data comes from the seeded generator: **development seeds 0–99,999** for building and tuning; **evaluation seeds 100,000–199,999** for the registered benchmark only. The harness refuses evaluation fixtures unless explicitly allowed for the registered run.

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

- **A fixture set per pipeline** has **180 batches**: 12 per defect class, spread evenly over that class's rates, plus **36 clean control batches**. A P1 batch is 100,000 transactions. P2 and P3 batches are the posted ones among them (about 95%), because only posted transactions reach the warehouse:
  - P2 delivers a warehouse load, `fact_transaction`, with `ledger_seq` and `committed_at`;
  - P3 delivers a chunk of the feature table, `fraud_features`, scored by the fraud model.
- **Every class on every pipeline** (author's decision, 2026-11-24). P2 takes the classes directly. In P3:
  - D04 writes text into `score`;
  - D05 makes `amount` negative or `score` fall outside [0, 1];
  - D06 puts a `merchant_category` outside the merchants catalogue;
  - D10 makes `scored_at` late;
  - D12 fans a transaction out to two account-dimension rows (the same `txn_id` with a different `account_type`). Its answer key names both `txn_id` and `account_type`, so an arm that checks key uniqueness is credited.

  Approved by the author on 2026-11-25, with the S01–S20 table.
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

**Scenarios S01–S20** use these alone or in sequence, each pre-registered with its target, duration, blast radius, recovery probes and RTO/RPO objectives. Objectives are set from SandboxBank's design before any run and never adjusted afterwards. Every fault stays within the environment's 15-minute ceiling and runs only on SANDBOX-01. Each scenario is repeated 10 times, in random order, starting at a random offset of 0–60 s into a delivery, so recovery is never timed against a predictable workload.

| ID | Fault type | Target (pipeline) | Parameters | Duration | RTO objective | RPO objective |
| --- | --- | --- | --- | --- | --- | --- |
| S01 | latency | `ledger-db-proxy` (P1) | +200 ms | 5 min | 10 min | 0 |
| S02 | latency | `ledger-db-proxy` (P1) | +2,000 ms | 5 min | 10 min | 0 |
| S03 | timeout | `ledger-db-proxy` (P1) | all calls time out | 2 min | 10 min | 0 |
| S04 | timeout | `analytics-db-proxy` (P2) | all calls time out | 5 min | 30 min | 0 |
| S05 | connection_drop | `ledger-db-proxy` (P1) | proxy disabled | 1 min | 15 min | 0 |
| S06 | connection_drop | `ledger-db-proxy` (P1) | proxy disabled | 5 min | 30 min | 0 |
| S07 | connection_drop | `analytics-db-proxy` (P2) | proxy disabled | 5 min | 30 min | 0 |
| S08 | network_partition | `payments-api` (P1) | disconnected from the sandbox network | 2 min | 15 min | 0 |
| S09 | network_partition | `etl-job` (P2) | disconnected from the sandbox network | 10 min | 45 min | 0 |
| S10 | latency | `model-api-proxy` (P3) | +2,000 ms per scoring call | 5 min | 15 min | 0 |
| S11 | container_stop | `payments-api` (P1) | stop | 1 min | 10 min | 0 |
| S12 | container_stop | `etl-job` (P2) | stop mid-load | 10 min | 45 min | 0 |
| S13 | connection_drop | `model-api-proxy` (P3) | proxy disabled: no scores | 5 min | 15 min | 0 |
| S14 | container_restart | `ledger-db` (P1) | restart | instant | 10 min | 0 |
| S15 | container_restart | `payments-api` (P1) | restart mid-delivery | instant | 5 min | 0 |
| S16 | container_restart | `etl-job` (P2) | restart mid-load | instant | 30 min | 0 |
| S17 | stale_upstream | `etl-job` (P2) | paused: settlement stops, state kept | 15 min | 30 min | 0 |
| S18 | container_restart | `feature-job` (P3) | restart mid-chunk | instant | 5 min | 0 |
| S19 | compound | `ledger-db-proxy`, then `payments-api` (P1) | +2,000 ms latency, then restart | 5 min | 15 min | 0 |
| S20 | compound | `ledger-db-proxy`, then `etl-job` (P1, P2) | connection drop, then paused ETL | 10 min | 45 min | 0 |

- **The author's review** (2026-11-25) approved every RTO as listed and set **RPO 0 for all twenty scenarios**, decision REGISTRY-SEAL:
  - **P1:** the ledger commits before payments-api acknowledges a write, so no acknowledged write may be lost.
  - **P2 and P3:** RAI's RPO is read on the ledger and labelled so. Data lost downstream is caught by the consistency checks that run after recovery (FR-08.3).
- **P3 rows.** S10, S13 and S18 target the P3 fraud feature pipeline, specified on 2026-11-21 once P3 ran as a service. P3 stores a chunk only together with its scores and its watermark move, and recomputes from the warehouse. So a scoring outage or a restart delays features but loses none. In S10 and S13 the feature-job must retry until scoring returns; in S18 it must resume with nothing lost or doubled.
- **Compound scenarios.** S19 and S20 are faults in sequence within one scenario's blast radius. If the chaos path cannot run them when RAI v1.0 is frozen, they are dropped and logged as a deviation, not replaced.
- **How P2 and P3 recovery is probed** (the author's review, Q1). The ETL and the feature-job each serve a freshness probe, `/readyz`. It answers 200 only while the job is caught up: its last cycle succeeded, drained its backlog, and finished within two minutes. So RTO for a P2 or P3 scenario runs until the job has caught up again, not merely until its process is back.
- **What each scenario must show.** In S04, S07 and S09 the database becomes unreachable and the ETL must retry until the fault clears. In S12 and S16 the ETL is stopped or restarted mid-load and must resume with nothing lost or doubled.
- **The registry is sealed.** The table is `benchmarks/registry/chaos-registry.yaml` (`rai-bench-chaos-registry/1`, B-28), with each scenario's blast radius, faults, recovery probes and objectives in the API's scenario shape, and 10 repetitions each. Its canonical SHA-256, the `registry_sha256` of any plan built from it, is **`01483388aec94ed63ebd0e953dba6b704172929a23f0ec3a701a7e35b6b4b06c`** (sealed 2026-11-25). `tests/unit/test_chaos_registry.py` fails if it changes. A change after the seal is a deviation (section 10).
- **The evaluation fault plan** is built by the author at registration, with an evaluation seed chosen privately (`rai-bench chaos-plan … --seed <100,000–199,999>`). Only its fault-log SEAL is committed and registered; the seed and the log stay with the author until the analysis, so no arm can learn the schedule.

## 6. Metrics

Every metric is computed by a script from raw logs, and published with its numerator and denominator:

- **Recall:** seeded defect instances (batch × class) with at least one matching alert, over seeded instances; overall and per class.
- **Precision:** alerts matching a seeded defect, over all alerts. Provisional before adjudication; final after.
- **False alarms on clean controls:** clean batches with any alert, over clean batches.
- **Detection lead time:** first matching alert minus the batch's delivery (onset = batch arrival = window end), in seconds.
- **Validation latency:** P50 and P95 wall seconds per batch (nearest-rank, so every value was observed), CPU seconds and peak memory.
- **Recovery:** RTO and RPO per scenario against its objectives, with the number of runs meeting them over runs attempted. RPO ground truth comes from the seeding log of acknowledged writes (B-30), not from the system under test.
- **RTO and RPO measurement error (H3):** RAI's value minus ground truth, per injection. Ground-truth RTO comes from an independent observer (B-44): a separate container, part of no arm, that probes the payments API and runs the consistency query (the ledger head, read straight from ledger-db) every 250 ms on its own monotonic clock. It applies RAI's restoration rule to its own samples, so the error measures RAI's measurement, not a different definition. An injection the observer did not watch is excluded and reported, never counted as agreement (`rai-bench h3`).
- **Post-recovery damage (H4):** injections with lost, duplicated or unexpected rows after recovery, and which arms flag them.
- **Drift (H6):** scoring windows from shift onset to the first drift alert, and false alarms on unshifted windows.

## 7. Statistical analysis

- Paired comparisons on the same batches: **McNemar's test** for detection (per defect instance), **Wilcoxon signed-rank** for lead time and latency, and **bootstrap** confidence intervals for recall and precision.
- All tests are two-sided. **Holm correction** across H1–H3 at a family-wise α of 0.05. No interim looks.
- The tests are written before the protocol is frozen and run on dry-run data first.
- Sensitivity analyses: by defect class, by rate, by scenario and by pipeline. All breakdowns are labelled exploratory.

| Hypothesis | Test | Effect size | Supported if |
| --- | --- | --- | --- |
| H1 | Exact McNemar, RAI against the better baseline, stratified by pipeline | Recall difference, 95% cluster bootstrap CI (10,000 resamples, clustered by batch) | Holm-adjusted p < 0.05, and RAI's precision is not lower by more than 0.05 (one-sided 95% CI) |
| H2 | Paired Wilcoxon signed-rank on instances both arms detect; non-detections censored at window end | Hodges–Lehmann median difference, bootstrap CI | Holm-adjusted p < 0.05 and the median difference favours RAI |
| H3 | Share of injections with absolute RTO error ≤ 2 s; exact RPO match rate | Clopper–Pearson 95% CI; Bland–Altman plot of RTO error | Point estimate ≥ 95% and lower 95% bound ≥ 90% |
| H4–H6 | Descriptive | Rates and medians with 95% CIs | Reported, not used for a headline claim |

**Sample and power.** Three pipelines (P1, P2, P3; P3 joined on 2026-11-21) give 540 batches: 432 seeded defect instances and 108 clean controls, 1,620 batch validations across three arms. With 20 scenarios × 10 repetitions there are 200 injections. If about 15% of the 432 paired instances are discordant (about 65 pairs) and RAI wins 70% of them, an exact McNemar test at the strictest Holm level (α = 0.0167) has roughly 80% power. If discordance is low, the result is reported with its interval as inconclusive, never topped up with extra runs.

## 8. Alerts, matching, sealing and adjudication

- **Common alert format.** "Each arm writes alerts in one common format: time, batch or injection, location, message." One JSON object per line (`rai-bench-alert/1`): arm, subject (batch or injection), time, location, message and an optional claimed defect class.
- **Matching rule, fixed before any run:** an alert matches a seeded defect if it names the same batch and either the affected column or the defect class. The scorer applies it automatically before human review.
- **Sealing.** The SHA-256 of each answer key (its SEAL) and of the fault log are **committed publicly before any arm runs**. The scorer refuses a key that no longer matches its seal.
- **Blind adjudication.** Arms are replaced by random codes; adjudicators relabel alerts without knowing which arm raised them.
- **Roles.** Reviewer and adjudicator roles are defined by criteria, not by name, and a key custodian holds the answer keys until scoring (decision PROTO).

## 9. Reference environment

Every result file records the environment: OS, CPU count, Python and package versions, the `uv.lock` hash and the git commit. Local paths and user names are never recorded, because results are published. Performance targets (NFR-PERF-1: 100k rows × 30 rules under 10 s) are confirmed on an 8 vCPU reference machine.

## 10. Registration and deviations

The protocol is registered on OSF under embargo before the evaluation runs (decision PROTO), with the answer-key and fault-log seals, and its SHA-256 is committed to the repository as a second timestamp. Any deviation after registration is recorded with its date, section, reason and who decided, and reported with the results.

**Order that never changes, whatever the pace:** baselines frozen (B1 by day 45, B2 by day 50) → protocol registered (by day 60) → RAI v1.0 frozen (by day 90) → benchmark runs → blind adjudication → replication.

**Independent replication.** One reviewer with no stake in RAI reruns a fixed subset on their own machine: all twelve defect classes on P1 (180 batches × 3 arms) and five scenarios (S03, S06, S11, S14, S17) × 3 repetitions. **Success, fixed now:** at least 95% of verdicts identical, every RPO identical, every RTO within ± 2 s of the original, and identical hashes for deterministic outputs. The reviewer also reviews baseline fairness, the matching rule and the analysis code, and signs a written assessment that is published whether it is positive or negative.

## 11. Limitations

- SandboxBank is synthetic; results show behaviour on a controlled system, not on a production bank.
- The author built RAI and the protocol, so there is self-evaluation bias (threat T-06). It is reduced by sealed keys, frozen baselines, external baseline review and blind adjudication, not removed.
- Fault injection is limited to Docker Compose targets (ADR-003).

| Type | Threat | Mitigation |
| --- | --- | --- |
| Internal | The author built RAI and the baselines and may favour RAI | External review before freezing; equal development budget; effort reported per arm |
| Internal | RAI tuned to the evaluation defects | Separate seed ranges; protocol and RAI v1.0 frozen before evaluation; hashes committed |
| Internal | Scoring bias | Blind adjudication by an external reviewer; a random 20% labelled twice, Cohen's κ reported |
| Construct | Seeded defects cleaner than real ones | Mixed rates (0.01–1%), compound scenarios, classes tied to known real-world causes |
| Construct | The ground-truth observer shares RAI's host | Separate container and clock; 10 injections spot-checked against an external stopwatch log |
| External | One synthetic system, two pipelines, one machine | Claims limited to this setting; replication on other hardware; pilot later (ADR-008) |
| Conclusion | Multiple comparisons | Only H1–H3 confirmatory, Holm-corrected; breakdowns labelled exploratory |
| Conclusion | Repeated injections are not independent | Random order and offsets; cluster bootstrap by scenario and batch |

## Open before registration

- Done: P3 joins the study (2026-11-21); P2 and P3 fixtures and baselines (2026-11-24); the author's review of every objective and the S01–S20 registry sealed (2026-11-25).
- The external review of baselines B1 and B2 for all three pipelines, then their freezes (day 45 and day 50).
- The evaluation fault plan and the evaluation fixture sets, built by the author with privately chosen evaluation seeds; their SEALs committed and registered on OSF (day 60).
