# Baselines B1 and B2: review packet

**For:** the external engineer who reviews the baselines before they are frozen (Validation & Benchmark Protocol, section 3).
**Status:** draft for review, 2026-11-14. B1 freezes on day 45 and B2 on day 50, after your review.

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

This packet covers the data-defect checks. The failure-scenario halves (B1 reruns integration tests every 60 s; B2 runs a 1-second health monitor) come with the chaos items.

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
| B1 | 23 tests (16 per batch, 7 per merchants file) | 119 lines of test code |
| B2 | 52 expectations (33 per batch, 19 per merchants file): 45 profiled, 7 added, 1 replaced, 1 removed | Profiler rules and hand edits in `baselines.py` |
| R | 36 rules per batch, 15 per merchants file, 22 for accounts | 53 lines of contract YAML |

**Author hours.** All three arms, RAI included, were drafted by an AI assistant (Claude Code) and are owned and reviewed by Trevor Kauyu. Review hours per arm: _to be recorded before the freeze_.

## Smoke run (development seeds; not the benchmark)

Run once after both baselines were written, to check that every arm runs end to end at protocol size: development seeds 15,001 and 15,002, one batch per defect class, 3 clean controls, 100,000 rows.

| Arm | Recall | Provisional precision | False alarms on clean batches | Latency p50 |
| --- | --- | --- | --- | --- |
| B1 | 7/12 | 7/7 | 0/3 | 1.5 s |
| B2 | 11/12 | 16/22 | 0/3 | 2.4 s |
| R | 12/12 | 21/24 | 0/3 | 1.4 s |

**No baseline rule was changed after this run.** Fixes before it addressed only checks that could not run or that failed on clean data: the type lists, R1 and the timestamp window. Each is recorded above.

## Questions for you

1. **Conflict of interest.** The same assistant drafted RAI and both baselines. Do the baselines look like an honest best effort?
2. **Development asymmetry.** RAI was developed while scoring against development defect fixtures (development seeds only). The protocol limits the baselines to the specification and clean batches. Should the baselines get the same development fixtures before the freeze?
3. **B1 scope.** B1 follows the protocol literally (required, types, primary key). Is that representative of "what a small institution typically has", or should it include range or reference checks?
4. **Profiling.** Is the profiler emulation (rules T1 to C5) a fair stand-in for GX's former auto-profiler? Is the 20-value set threshold reasonable?
5. **Hand edits.** Are K1 to K7 what a competent GX team would add from this specification? Is anything missing, or does anything go beyond the specification? K6 (batch id) and K4 (window) use the delivery's arrival time, which every arm receives.
6. **Exact row count.** The profiled row-count range is exactly 100,000. Keep it as the profiler made it, or loosen it?
7. **Reading choices.** pandas below 3, because GX 1.24 misreads pandas 3 string types; the timestamp column parsed on read.

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
