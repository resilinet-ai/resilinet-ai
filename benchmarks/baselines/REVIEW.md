# Baselines B1 and B2: review packet

**For:** the external engineer who reviews the baselines before they are frozen (Validation & Benchmark Protocol, section 3).
**Status:** draft for review, 2026-11-14; P2 and P3 added 2026-11-24. B1 freezes on day 45 and B2 on day 50, after your review.

The protocol compares RAI (arm R) with two baselines "so a win cannot come from a weak strawman". Please judge whether B1 and B2 are fair, competent versions of what they claim to be. Change requests are welcome; every change before the freeze is recorded here.

## What the protocol fixes

| Arm | What it represents | Data-defect checks (section 3) |
| --- | --- | --- |
| B1 | What a small institution typically has today | pytest tests written from the pipeline's requirements: required fields, types, a primary-key check per batch |
| B2 | A competent team using a standard open-source tool | A Great Expectations suite auto-profiled on 30 clean batches, then hand-tightened (documented) |

**Fairness rules (section 3):**
- All arms see the same batches at the same time on the same machine.
- Rules come only from the pipeline specification and 30 clean development batches, never from evaluation fixtures.
- Rule counts, lines of code and author hours are reported per arm.

The specification is SandboxBank schema v1: [`sandboxbank/README.md`](../../sandboxbank/README.md) ("Schema v1") and [`schema.py`](../../sandboxbank/src/sandboxbank/schema.py).

This packet covers the data-defect checks and, since 2026-11-25, the failure-scenario halves (B1 reruns integration tests every 60 s; B2 runs a 1-second health monitor): see "During the chaos benchmark" below.

## B1: the conventional suite

[`b1/test_feed.py`](b1/test_feed.py) is an ordinary pytest file, run once per delivered batch in its own process, as a CI job runs it. Each failing test becomes one alert.

| Test | What it checks | Per file |
| --- | --- | --- |
| `test_columns` | The columns are exactly the specified ones | 1 |
| `test_required` | Every required field has a value | 10 (transactions), 5 (merchants) |
| `test_type` | Values parse as their specified type: UUID, decimal, timestamp with zone, boolean | 4 (transactions) |
| `test_primary_key` | `txn_id` (or `merchant_id` in the merchants file) is unique within the file | 1 |

**Not checked, by the protocol's definition of B1:** value ranges, references to accounts or merchants, freshness, or anything across batches.

## B2: Great Expectations

**Tool:** Great Expectations 1.24.1 with pandas 2.3.3. The files read like a pandas team reads a CSV: default type inference, with the specified timestamp column parsed.

**Profiling.** GX 1.x has no profiler (it was removed in 1.0), so [`profile_suite`](../src/rai_bench/baselines.py) applies the classic profiler's rules, openly:

| Rule | Expectation | When |
| --- | --- | --- |
| T1 | Columns match the observed ordered list | Always |
| T2 | Row count between the observed minimum and maximum | Always |
| C1 | Values in the observed types | Every column |
| C2 | Not null | No batch had a null |
| C3 | Unique | Every batch was unique |
| C4 | In the observed value set | At most 20 distinct values, not boolean |
| C5 | Between the observed minimum and maximum | Numeric columns |

It was profiled on 30 clean development batches of 100,000 rows (development seed 45,000; `rai-bench b2-profile`). The profiled suites are kept beside the final ones so the hand edits can be diffed: [`b2/`](b2/).

**Hand edits** (`TIGHTENINGS`). Each one quotes the specification:

| ID | Change | From the specification | Why |
| --- | --- | --- | --- |
| K1 | `account_id` in the accounts reference | "References accounts.account_id" | |
| K2 | `merchant_id` in the merchants reference | "References merchants.merchant_id" | |
| K3 | `txn_id` matches the UUID format | "txn_id (UUID)" | |
| K4 | `created_at` inside the batch window (arrival minus 900 s, to arrival) | "inside the batch window"; window 900 s | |
| K5 | `amount` from 0.01 to the largest account `txn_limit` (**replaces** the profiled range) | "Positive, 2 dp, at most the account txn_limit" | The profiled range is over-fitted to 30 batches. A per-account limit needs a custom expectation. |
| K6 | `batch_id` is the delivered batch | "batch_id: B followed by 6 digits" | A delivered file should carry its own batch id |
| K7 | Merchants: `merchant_id` matches `MER-` and 6 digits | "MER- followed by 6 digits" | |
| R1 | **Removes** the type check on `created_at` | "created_at (UTC, ms)" | GX 1.24 cannot match the type of a time-zone-aware timestamp column, so the check failed on every clean batch |

**When an expectation cannot run** on a column that exists (for example, a timestamp column that could not be parsed), B2 raises a "could not check" alert, as arm R does for a rule that cannot run. On a missing column, only the column-list expectation reports.

**Not checked:** duplicates across batches (GX keeps no state between batches) and per-account amount limits.

## Defect-class claims

Every arm may claim a defect class (section 8). Claims come from what each check is, read against the public defect catalogue (section 4), exactly as arm R's do:

| Check | B1 | B2 |
| --- | --- | --- |
| Duplicate key in a batch / in the merchants file | D01 / D12 | D01 / D12 |
| Missing value | D03 | D03 |
| Wrong type or format, or could not check | D04 | D04 |
| Out-of-range amount | | D05 |
| Unknown account or merchant | | D06 |
| Column renamed (one missing, one unexpected) / dropped | D07 / D08 | D07 / D08 |
| Outside the batch window: some rows / every row | | D10 / D11 |
| Not the delivered batch | | D11 |

## Effort (from `rai-bench effort`)

| Arm | What the authors wrote | Size |
| --- | --- | --- |
| B1 | 67 checks: 16 per P1 batch, 20 per P2 load, 24 per P3 chunk, 7 per merchants file | 153 lines of test code |
| B2 | 139 expectations (33 per P1 batch, 37 per P2 load, 50 per P3 chunk, 19 per merchants file): 118 profiled, 21 added, 5 replaced, 4 removed | Profiler rules and hand edits in `baselines.py` |
| R | 36 rules per P1 batch, 37 per P2 load, 42 per P3 chunk, 15 per merchants file, 22 for accounts | 122 lines of contract YAML |

**Author hours.** All three arms, RAI included, were drafted by an AI assistant (Claude Code) and are owned and reviewed by Trevor Kauyu. Review hours per arm: _to be recorded before the freeze_.

## Smoke run (development seeds; not the benchmark)

Run once after both baselines were written, to check that every arm runs end to end at protocol size: development seeds 15,001 and 15,002, one batch per defect class, 3 clean controls, 100,000 rows.

| Arm | Recall | Provisional precision | False alarms on clean batches | Latency p50 |
| --- | --- | --- | --- | --- |
| B1 | 7/12 | 7/7 | 0/3 | 1.5 s |
| B2 | 11/12 | 16/22 | 0/3 | 2.4 s |
| R | 12/12 | 21/24 | 0/3 | 1.4 s |

**No baseline rule was changed after this run.** Fixes before it addressed only checks that could not run or that failed on clean data: the type lists, R1 and the timestamp window. Each is recorded above.

## P2 and P3 (added 2026-11-24, for the same review)

The author decided that pipelines P2 and P3 join the study (B-28), and that their baselines go into this review, before the same freezes.
- **P2** delivers warehouse loads, `fact_transaction`.
- **P3** delivers chunks of the fraud feature table, `fraud_features`.

Both are written the same way as P1's: from SandboxBank schema v1 ([`schema.py`](../../sandboxbank/src/sandboxbank/schema.py)) and 30 clean development batches, before either suite ran on a defect fixture.

**One specification change, before any baseline saw a fixture.** Two schema descriptions now state a property that already held by construction, as P1's spec does for `created_at`:
- P2 `created_at` is "inside the load's batch window";
- P3 `scored_at` is "inside the batch window, or at most 10 s after it".

**B1.** The same four tests, with two more tables in `SPEC`, chosen per delivery by `RAI_B1_TABLE`:
- `fact_transaction`: 20 checks;
- `fraud_features`: 24 checks.

P1's checks are unchanged.

**B2.** Each table was profiled on 30 clean development deliveries of 100,000 transactions (development seed 45,000, as for P1; `rai-bench b2-profile --pipeline P2|P3`). P2 and P3 batches hold the posted rows only, about 95% of them.
- `fact_transaction`: 37 expectations, 30 profiled.
- `fraud_features`: 50 expectations, 43 profiled.

Hand edits:

| Table | ID | Change | From the specification |
| --- | --- | --- | --- |
| P2 | K1, K2 | `account_id` and `merchant_id` in the references | "References accounts.account_id" / "merchants.merchant_id" |
| P2 | K3 | `txn_id` matches the UUID format | "txn_id (UUID)" |
| P2 | K4 | `created_at` inside the batch window | "inside the load's batch window" |
| P2 | K5 | `amount` from 0.01 to the largest `txn_limit` (**replaces** the profiled range) | "Positive, 2 dp, at most the account txn_limit" |
| P2 | K8 | `committed_at` later than `created_at` | "When the ledger committed it; after created_at" |
| P2 | K9 | `ledger_seq` increasing | "The ledger's sequence number; increases" |
| P2 | R1, R2 | **Remove** the type checks on `created_at` and `committed_at` | As P1's R1: GX 1.24 cannot type a time-zone-aware column |
| P3 | K3 | `txn_id` matches the UUID format | "txn_id (UUID)" |
| P3 | K4 | `scored_at` from the window's start to 10 s after its end | "inside the batch window, or at most 10 s after it" |
| P3 | K5 | `amount` at least 0.01 (**replaces** the profiled range) | "Positive, 2 dp" |
| P3 | K10 | `merchant_category` in the merchants catalogue | "A category of the merchants catalogue" |
| P3 | K11 | `score` from 0 to 1 (**replaces**) | "The model's fraud probability, in [0, 1]" |
| P3 | K12 | `amount_to_limit` above 0, at most 1 (**replaces**) | "amount / the account's txn_limit, in (0, 1]" |
| P3 | K13 | `model_recipe_sha256` is 64 hex characters | "SHA-256 of the model recipe" |
| P3 | R1 | **Removes** the type check on `scored_at` | As P1's R1 |

**Claims.** As P1's, read from what each check is:
- K10 (unknown category) claims D06.
- K11 and K12 (out of range) claim D05.
- K8 and K9 claim no class; their alerts still match a defect through a shared column.

**The author's P3 defect mapping** affects how all arms are scored, not only R:
- D06 is a category outside the catalogue;
- D12 is a transaction fanned out to two account rows, and its answer key names `txn_id` and `account_type`.

So an arm that checks key uniqueness is credited for D12 on P3.

**Smoke run** (development seeds 15,001 and 15,002; one batch per class, 3 clean controls, 100,000 transactions). It ran once, after the suites were written. **No rule was changed after it**: no check failed to run, and no clean batch raised an alarm.

| Arm | P2 recall | P2 provisional precision | P3 recall | P3 provisional precision | False alarms (P2, P3) |
| --- | --- | --- | --- | --- | --- |
| B1 | 7/12 | 7/7 | 7/12 | 7/7 | 0/3, 0/3 |
| B2 | 11/12 (misses D02) | 16/21 | 10/12 (misses D02, D11) | 16/21 | 0/3, 0/3 |
| R | 12/12 | 20/23 | 12/12 | 18/20 | 0/3, 0/3 |

**Why B2 misses D11 on P3.** The re-sent previous chunk was partly scored in the seconds after its own window closed, so some of its rows fall inside the new window allowed by K4's 10 s grace. B2 therefore sees *some* rows out of window and claims D10, not D11, and the alert matches nothing. This is a consequence of the rule as written from the specification, left as is; please say whether you see it differently.

## During the chaos benchmark (added 2026-11-25)

While faults are injected (Protocol section 5), the baselines watch the live system as a team would. The author decided three things before either half was written:
- B1 is read-only;
- B2 watches liveness only;
- the injection matching rule (Protocol section 8).

**B1, every 60 s** ([`b1/test_live.py`](b1/test_live.py), its own pytest process per run, read-only):

| Test | Checks | Names |
| --- | --- | --- |
| `test_payments_api_is_ready` | payments-api `/readyz` is 200 | `payments-api` |
| `test_ledger_head_is_readable_and_never_falls` | `/v1/ledger/head` answers, and `seq` never falls between runs | `ledger-db` |
| `test_warehouse_answers` | analytics-db answers a query | `analytics-db` |
| `test_warehouse_keeps_up_with_the_ledger` | The warehouse's newest row is at most 5 min behind the ledger's | `etl-job` |
| `test_feature_table_keeps_up_with_the_warehouse` | The feature table is at most 5 min behind the warehouse | `feature-job` |

Each failing test in a run is one alert. Five minutes allows a few of the ETL's 60 s cycles, as a team writing these checks from the README would.

**B2, every 1 s** (`rai_bench.live.B2Monitor`): payments-api `/readyz`, and `/healthz` of the ETL and the feature-job. It raises one alert when a component goes from healthy to failing, and the next only after it has been healthy again, as paging monitoring does.

B2 does not watch the jobs' freshness `/readyz`. Health-check-only monitoring is what H4 compares RAI against, so a job that is alive but behind looks healthy to B2. That is the gap H4 tests.

**Matching** (Protocol section 8):
- an alert matches an injection if it is raised between the fault's onset and five minutes after the observer's restoration;
- it must name a component of the injection's blast radius or recovery probes;
- any other alert during a run is a false alarm.

## Questions for you

1. **Conflict of interest.** The same assistant drafted RAI and both baselines. Do the baselines look like an honest best effort?
2. **Development asymmetry.** RAI was developed while scoring against development defect fixtures (development seeds only). The protocol limits the baselines to the specification and clean batches. Should the baselines get the same development fixtures before the freeze?
3. **B1 scope.** B1 follows the protocol literally (required, types, primary key). Is that representative of "what a small institution typically has", or should it include range or reference checks?
4. **Profiling.** Is the profiler emulation (rules T1 to C5) a fair stand-in for GX's former auto-profiler? Is the 20-value set threshold reasonable?
5. **Hand edits.** Are K1 to K7 what a competent GX team would add from this specification? Is anything missing, or does anything go beyond the specification? K6 (batch id) and K4 (window) use the delivery's arrival time, which every arm receives.
6. **Exact row count.** The profiled row-count range is exactly 100,000. Keep it as the profiler made it, or loosen it?
7. **Reading choices.** pandas below 3, because GX 1.24 misreads pandas 3 string types; the timestamp column parsed on read.
8. **P2 and P3.** Are the new hand edits (P2 K1–K9, P3 K3–K13) what a competent team would add from those specifications? Was it fair to state the window properties of P2 `created_at` and P3 `scored_at` in the specification before the suites were written?
9. **The chaos halves.** Is a 60 s read-only integration suite, with a 5-minute lag allowance, what a small institution would run? Is a liveness-only 1 s monitor a fair stand-in for health-check monitoring?

## How to run

```bash
uv sync --all-packages --all-extras                  # B1 needs pytest, B2 Great Expectations
uv run sandboxbank fixtures --out data/fx --data-seed 15001 --injector-seed 15002 --per-class 1 --clean 3
uv run rai-bench run data/fx --out benchmarks/results/review-1 --arm B1 --arm B2 --arm R
uv run rai-bench score data/fx benchmarks/results/review-1
uv run rai-bench effort
```

## Sign-off

| Arm | Reviewer | Date | Changes requested | Frozen at (commit) |
| --- | --- | --- | --- | --- |
| B1 | | | | |
| B2 | | | | |
