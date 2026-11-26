# Benchmarks

Harness and analysis for the Validation & Benchmark Protocol. Results are written to `benchmarks/results/` (git-ignored until published).

The `rai-bench` package (`benchmarks/src/rai_bench/`) runs the comparison arms on a sealed SandboxBank fixture set, records every arm's alerts in one common format, and scores them against the answer key.

## Try it

```bash
make benchmark        # = uv run rai-bench dry-run
```

The dry run builds a small development fixture set (one batch per defect class, three clean controls, development seeds only), runs arm R, scores it and prints a summary:

```text
arm R: recall 12/12, provisional precision 21/24, false alarms 0/3 clean batches, ...
  classes missed at least once: none
```

Step by step:

```bash
# small set; drop the last three options for the full protocol size (180 x 100k rows)
uv run sandboxbank fixtures --out data/fx --data-seed 1 --injector-seed 2 \
    --rows 10000 --per-class 1 --clean 3
uv run rai-bench run data/fx --out benchmarks/results/run-1
uv run rai-bench score data/fx benchmarks/results/run-1
```

| Exit code | Meaning |
| --- | --- |
| `0` | Done |
| `2` | Could not run: bad input, broken seal, results folder not empty, or evaluation fixtures without `--allow-evaluation` |

## Common alert format (Protocol section 8)

Every arm writes `alerts/<arm>.jsonl`, one JSON object per line:

| Field | Meaning |
| --- | --- |
| `format` | `rai-bench-alert/1` |
| `arm` | `B1`, `B2` or `R` (replaced by random codes when alerts are blinded) |
| `subject` | `{"kind": "batch" or "injection", "id": ...}` |
| `time` | When the arm raised the alert, UTC, `...Z` |
| `location` | Columns (batch) or components (injection) named; may be empty |
| `message` | Free text for the adjudicator |
| `defect_class` | Optional: the arm's own claim, `D01` to `D12`, or `null` |

**Matching rule (fixed in Protocol section 8):** an alert matches a seeded defect if it names the same batch and either the affected column or the defect class. The scorer applies it automatically before human review. Every arm may claim a class, so a whole-batch defect with no column (D11, stale feed) can still be matched.

## How a run works

1. **Deliver.** The runner gives each batch in `feed/` to every arm in turn, with its arrival time from SandboxBank's public schedule. Arms never see `answer_key/`; a test runs the harness on a copy without one.
2. **Record.** For each arm and batch the runner writes delivery time, wall and CPU seconds, and the alerts raised. `run.json` records the fixtures (seeds, answer-key hash), each arm's version and contract hashes, and the environment: OS, CPU count, Python and package versions, `uv.lock` hash, git commit. No local paths or user names are recorded.
3. **Score.** The scorer checks the answer key against its SEAL and the manifest, then computes, with numerator and denominator: recall (overall and per class), provisional precision, false alarms on clean controls, detection lead time, and P50/P95 latency. It also writes one automatic label per alert for the adjudicator.

**Safety rails:**
- Feed files must match their manifest hashes.
- Results folders are never overwritten.
- Evaluation fixtures (seeds 100,000 to 199,999) are refused unless `--allow-evaluation` is given, for the registered benchmark only.

## Arms

| Arm | Status |
| --- | --- |
| `R`, RAI | Adapter in `rai_bench.arms`: the SandboxBank contracts through the validation engine with a persistent key history. One alert per rule that does not pass, and one per breaking schema change. |
| `B1`, conventional suite | Drafted for review (B-21): the pytest file [`baselines/b1/test_feed.py`](baselines/b1/test_feed.py), run once per delivery. Frozen on day 45 after external review |
| `B2`, Great Expectations | Drafted for review (B-21): suites profiled on 30 clean development batches, then hand-tightened from the specification ([`baselines/b2/`](baselines/b2/)). Frozen on day 50 |

The review packet, with what each baseline checks, the hand edits, effort counts and open questions for the reviewer, is [`baselines/REVIEW.md`](baselines/REVIEW.md). The baselines need the `b1` and `b2` extras: `uv sync --all-packages --all-extras`.

```bash
uv run rai-bench run data/fx --out benchmarks/results/run-2 --arm B1 --arm B2 --arm R
uv run rai-bench effort          # rule counts and lines of code per arm (Protocol section 3)
uv run rai-bench b2-profile --work data/b2-dev   # regenerate B2's suites (development seed only)
```

Fault injections (scenarios S01 to S20) use the same alert format with `subject.kind = "injection"` and the injection id from the fault plan below. Scoring them comes with the chaos benchmark runner.

## Chaos benchmark: sealed fault plans (B-28)

The chaos benchmark's answer key is its **planned fault log**: which registered scenario is injected in which order, and the earliest each may start. Like a defect answer key, its SHA-256 (the SEAL) is committed publicly before any arm runs (Protocol section 8, threat T-06).

```bash
uv run rai-bench chaos-plan scenarios.yaml --out data/chaos-plan --seed 3   # development seed
uv run rai-bench chaos-seal data/chaos-plan --expect <publicly committed SEAL>
```

The registry (YAML or JSON) is written by the author: format `rai-bench-chaos-registry/1`, then for each scenario an `id` (`S01` to `S20`), a `title`, how many `repetitions`, and its `definition` in the same shape the RAI API takes for a scenario version (blast radius, faults in order, recovery plan). Every scenario must declare recovery probes and **both** RTO and RPO objectives, so no objective is chosen after the results.

`chaos-plan` writes:

| File | What |
| --- | --- |
| `registry.json` | The registry, canonical, with each scenario's SHA-256. Public: arms may read it |
| `answer_key/fault_log.jsonl` | One line per injection (`rai-bench-fault/1`): injection id, scenario and its hash, repetition, earliest start (seconds from the run's start), faults, components touched, objectives |
| `answer_key/SEAL` | SHA-256 of the fault log |
| `manifest.json` | Seed, gaps, registry hash, SEAL and every file's hash |

Repetitions are shuffled by the seed, and a seeded gap (60 to 600 s by default) follows each injection's faults and recovery window, so arms cannot predict when a fault lands. Seeds follow the protocol ranges; anything that later runs or scores a plan loads it with `rai_bench.chaos.load_sealed_plan`, which refuses a broken seal and an evaluation plan unless explicitly allowed.

`chaos-seal` exits 1 if the fault log no longer matches its SEAL, the manifest or the public hash, or if a scenario in `registry.json` no longer matches the definition the log was planned from (a loosened RTO, say). Rewriting the log, its SEAL and the manifest together passes the local checks, which is why the SEAL must be committed publicly first.

**The sealed registry** is [`registry/chaos-registry.yaml`](registry/chaos-registry.yaml): S01–S20 as the author approved them on 2026-11-25. Its `registry_sha256` is `01483388aec94ed63ebd0e953dba6b704172929a23f0ec3a701a7e35b6b4b06c`, and `tests/unit/test_chaos_registry.py` fails if it changes. To build the evaluation plan at registration, choose an evaluation seed privately, run `rai-bench chaos-plan registry/chaos-registry.yaml --out <private folder> --seed <seed>`, and commit only the printed SEAL.

**The baselines during chaos** run from `rai_bench.live`: `B1Live` (B1's read-only integration tests, every 60 s) and `B2Monitor` (a 1 s liveness monitor). Both write common-format alerts naming components, and `assign` matches them to injections by Protocol section 8's rule.

### Running the chaos benchmark (`rai_bench.chaos_run`)

Three commands. Secrets come only from the environment, never the command line:

```bash
# 1. Publish S01-S20 in a fresh RAI project, have a reviewer approve them, and plan each one.
#    Needs RAI_BENCH_TENANT, RAI_BENCH_ENGINEER_TOKEN and RAI_BENCH_REVIEWER_TOKEN (two people).
uv run rai-bench chaos-setup <plan> --out <run>/setup.json --project <id> --environment-id <id>
# 2. Play the sealed plan, with B1 and B2 watching. Resumes if stopped.
#    Needs RAI_BENCH_API_KEY (runs:start, runs:read) and RAI_B1_ANALYTICS_DSN.
uv run rai-bench chaos-play <plan> <run>/setup.json --out <run> --expect <SEAL> [--allow-evaluation]
# 3. Score against the observer's log.
uv run rai-bench chaos-score <run> observations.csv --out <run>/score.json
```

- **Setup** publishes every scenario of the plan's public `registry.json` exactly as registered, and refuses if RAI stored one differently. Each scenario's plan is the scenario, then two post-recovery consistency checks (FR-08.3):
  - `bench.p2.consistency`: the warehouse's `fact_transaction` against the ledger's posted rows;
  - `bench.p3.consistency`: `fraud_features` against `fact_transaction`.

  They run after **every** injection, since a P1 fault can damage the pipelines downstream (the author's decision, 2026-11-26). The worker needs three connectors: `ledger`, `warehouse` (schema `warehouse`) and `features` (schema `features`). RAI is **not** given the seeding log: it is the ground truth for RPO and damage.
- **Play** starts each injection's run no earlier than its `start_offset_seconds` after the run began, with an `Idempotency-Key` per injection, and waits for it to finish. Meanwhile B1 and B2 write `alerts.jsonl`. Each injection becomes one line of `injections.jsonl` (`rai-bench-chaos-run/1`), with:
  - RAI's run state and verdict;
  - its recovery measurement (onset, revert, RTO, RPO, objectives met);
  - its consistency findings;
  - the scenario's recovery probes.

  A run that hasn't finished by its faults, window and checks plus 10 minutes stops the runner. Resolve the run, then resume.
- **Score** gives:
  - recovery attainment per scenario and overall: objectives met over runs **attempted**, so a refused, aborted or unmeasured run counts as not met;
  - H3's RTO half: each injection is judged on its own probes, and unmeasured injections are counted;
  - each baseline's detection by Protocol section 8: share of injections detected, median detection time and false alarms.

### Ground truth for RPO and damage (`rai_bench.truth`)

Run `sandboxbank seed` against the payments-api for the whole benchmark, and pass its log to play with `--acks <log>`. Play then reconciles SandboxBank **itself** after each injection's RAI run. It reads read-only, outside every arm, through `RAI_TRUTH_LEDGER_DSN` and `RAI_TRUTH_ANALYTICS_DSN`. It checks:

- **lost acknowledged writes:** writes in the seeding log (hash chain verified first) that are not in the ledger;
- **the warehouse against the ledger's posted rows:** missing, duplicated or unexpected rows, by `ledger_seq`;
- **the feature table against the warehouse:** the same three ways.

A job catching up is not damage. *Missing* counts only rows that existed upstream when the check began, and the check waits up to 900 s for them, as RAI's own checks do. Damage belongs to an injection only when it is **new**, not reported after an earlier one. The reported keys are kept in `truth-state.json`, so a resumed run attributes nothing twice.

`chaos-score` then adds two results, by the author's decisions of 2026-11-26:

- **H3's RPO half:** RAI matches when it agrees with the seeding log on whether any acknowledged write was lost (RAI's `lost_rows` > 0). The share comes with its Clopper-Pearson 95% interval; where both found a loss, the seconds difference is descriptive.
- **H4:** the injections that left new damage, and who flagged each one:
  - RAI flags it when a consistency check failed or its RPO found lost rows.
  - B1 or B2 flag it only with a matching alert raised **after** the observer's restoration, while the system looked healthy again. An outage alert during the fault is detection, not damage detection.

  `r_not_b2` counts the damage RAI flagged and B2 missed. Each arm's flags on undamaged injections are reported as well.

## Protocol H3: RTO against the independent observer (B-44)

`rai-bench h3` compares RAI's RTO for each injection with the ground truth from the observer's log (`sandboxbank observe`; see the SandboxBank README):

```bash
uv run rai-bench h3 rai-recovery.jsonl observations.csv --out benchmarks/results/run-1/h3.json
```

RAI's measurements are one JSON object per line, `rai-bench-recovery/1`, with these fields:

- `injection`: the injection's ID from the fault plan;
- `onset_at`, `reverted_at` and `rto_seconds`: as `GET /v1/assurance/recovery` reports them, with `null` when RAI found the system not restored;
- `window_seconds`: optional;
- `probes`: optional, the injection's recovery probes from the registry (`payments-health`, `etl-ready`, `features-ready`). Without it, `["payments-health"]`.

The observer's truth uses the same onset, revert, recovery probes and restoration rule. A P2 scenario recovered by `etl-ready` is judged on the observer's own check of the etl-job's `/readyz`, not the payments-api's.

| Outcome per injection | Counts as |
| --- | --- |
| Both found it restored, and they differ by at most 2 s | Agrees |
| Neither found it restored within the window | Agrees |
| One found it restored and the other did not, or they differ by more than 2 s | Disagrees |
| The observer was not watching (started after the revert, did not check one of the probes, or a gap of more than 5 s) | Excluded and counted as `unobserved`, never as agreement |

The output gives:

- the share that agrees, with its exact Clopper-Pearson 95% interval;
- the verdict: **supported** when the share is at least 95% and the interval's lower bound at least 90% (Protocol section 7);
- Bland-Altman bias and limits of agreement over the injections where both found a value.

H3's other half, the exact RPO match against the seeding log, comes from `recovery.rpo_truth` (B-30).

## Also in the harness

`rai-bench history-scale` times a 100,000-row validation against a cross-batch key history of protocol size: 18M keys, one pipeline's 180 batches. See [`analysis/`](analysis/README.md) for how metrics map to the protocol.
