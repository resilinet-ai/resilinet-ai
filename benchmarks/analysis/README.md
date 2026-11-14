# Analysis

Protocol section 6 says every metric is computed by a script from raw logs, with its numerator and denominator published. This folder is where that analysis lives.

| Protocol metric | Where it is computed | Status |
| --- | --- | --- |
| Recall, per class | `rai_bench.scoring` (`rai-bench score`) | Done (B-15) |
| Precision | `rai_bench.scoring`: provisional, before adjudication | Provisional (B-15); adjudicated labels later |
| False escalation / false alarms on clean controls | `rai_bench.scoring` | Done (B-15) |
| Detection lead time | `rai_bench.scoring`: first matching alert minus delivery | Done (B-15) |
| Validation latency P50/P95, CPU seconds | `rai_bench.scoring`, from `deliveries.jsonl` | Done (B-15); peak memory with containerised arms |
| RTO/RPO error, attainment, post-recovery damage | Needs the fault log and observer | Later (chaos agent and recovery evaluator) |
| Hypothesis tests: McNemar, Wilcoxon, cluster bootstrap, Holm | Scripts in this folder | Before the protocol freeze; run on dry-run data first (section 7) |

Percentiles use the nearest-rank method, so every reported value was actually observed.
