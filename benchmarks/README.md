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

Fault injections (scenarios S01 to S20) use the same alert format with `subject.kind = "injection"`; they are scored once the chaos agent writes its fault log.

## Also in the harness

`rai-bench history-scale` times a 100,000-row validation against a cross-batch key history of protocol size: 18M keys, one pipeline's 180 batches. See [`analysis/`](analysis/README.md) for how metrics map to the protocol.
