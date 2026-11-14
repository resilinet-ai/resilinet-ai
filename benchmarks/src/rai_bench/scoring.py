"""Automatic scoring of a run against the sealed answer key (Protocol sections 6-8; B-15).

Matching rule (Protocol section 8, fixed before any run): an alert matches a seeded
defect if it names the same batch and either the affected column or the defect
class. It is applied automatically before human review; adjudication can still
relabel an alert, so precision here is the provisional, pre-adjudication value.

Metrics (Protocol section 6), each with its numerator and denominator:

- recall: seeded defect instances (batch x class) with at least one matching
  alert, over seeded instances; also per class;
- provisional precision: alerts matching a seeded defect, over all alerts;
- false alarms on clean controls: clean batches with any alert, over clean batches;
- detection lead time: first matching alert minus delivery of the batch, seconds;
- validation latency: P50 and P95 wall seconds per batch, and CPU seconds.

Percentiles use the nearest-rank method, so every reported value is an observed one.
Hypothesis tests (McNemar, Wilcoxon, bootstrap) are added before the protocol is
frozen, and run on dry-run data first (Protocol section 7).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rai_bench.alerts import Alert, read_alerts
from rai_bench.runner import HarnessError, load_manifest
from sandboxbank.fixtures import verify_seal

SCORES_FORMAT = "rai-bench-scores/1"
MATCHING_RULE = "same batch, and a shared column or the same defect class (Protocol section 8)"


@dataclass(frozen=True, slots=True)
class Instance:
    """One seeded defect instance: a batch and its class."""

    batch_id: str
    defect_class: str
    columns: frozenset[str]


def load_truth(fixtures: Path) -> tuple[list[Instance], list[str]]:
    """Seeded instances and clean control batches, after checking the seal."""
    manifest = load_manifest(fixtures)
    if not verify_seal(fixtures, manifest.get("answer_key_sha256")):
        raise HarnessError("the answer key does not match its SEAL and manifest; not scoring")
    lines = (fixtures / "answer_key" / "ground_truth.jsonl").read_text("utf-8").splitlines()
    instances: list[Instance] = []
    clean: list[str] = []
    for line in lines:
        record = json.loads(line)
        if record["defect_class"] is None:
            clean.append(record["batch_id"])
        else:
            columns = frozenset(record["columns"])
            instances.append(Instance(record["batch_id"], record["defect_class"], columns))
    return instances, clean


def matches(alert: Alert, instance: Instance) -> bool:
    if alert.subject_kind != "batch" or alert.subject_id != instance.batch_id:
        return False
    named_column = bool(instance.columns.intersection(alert.location))
    return named_column or alert.defect_class == instance.defect_class


def percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile: the smallest observed value with at least ``share`` of
    the values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(share * len(ordered))) - 1]


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    value = round(numerator / denominator, 6) if denominator else None
    return {"numerator": numerator, "denominator": denominator, "value": value}


def _summary(values: list[float]) -> dict[str, Any]:
    p50, p95 = percentile(values, 0.5), percentile(values, 0.95)
    return {
        "n": len(values),
        "p50": None if p50 is None else round(p50, 6),
        "p95": None if p95 is None else round(p95, 6),
    }


def _deliveries(run_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = run_dir / "deliveries.jsonl"
    if not path.is_file():
        raise HarnessError(f"{run_dir} has no deliveries.jsonl; is it a harness run?")
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        found[(record["arm"], record["batch_id"])] = record
    return found


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def score_arm(
    arm: str,
    alerts: list[Alert],
    instances: list[Instance],
    clean: list[str],
    delivered: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Metrics for one arm, and one automatic label per alert."""
    labels: list[dict[str, Any]] = []
    first_match: dict[Instance, datetime] = {}
    matched_alerts = 0
    for index, alert in enumerate(alerts):
        hits = [i for i in instances if matches(alert, i)]
        if hits:
            matched_alerts += 1
        for hit in hits:
            if hit not in first_match or alert.time < first_match[hit]:
                first_match[hit] = alert.time
        labels.append(
            {
                "arm": arm,
                "alert": index,
                "subject": alert.subject_id,
                "matched": [f"{h.batch_id}/{h.defect_class}" for h in hits],
            }
        )

    by_class: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for instance in instances:
        by_class[instance.defect_class][1] += 1
        by_class[instance.defect_class][0] += instance in first_match
    alerted = {a.subject_id for a in alerts if a.subject_kind == "batch"}
    false_alarms = sum(batch in alerted for batch in clean)

    lead_times: list[float] = []
    for instance, moment in first_match.items():
        record = delivered.get((arm, instance.batch_id))
        if record is not None:
            lead_times.append((moment - _when(record["delivered_at"])).total_seconds())
    timings = [r for (name, _), r in delivered.items() if name == arm]
    metrics = {
        "recall": _ratio(len(first_match), len(instances)),
        "recall_by_class": {
            name: _ratio(found, total) for name, (found, total) in sorted(by_class.items())
        },
        "precision_provisional": _ratio(matched_alerts, len(alerts)),
        "false_alarms_on_clean_controls": _ratio(false_alarms, len(clean)),
        "lead_time_seconds": _summary(lead_times),
        "latency_wall_seconds": _summary([float(r["wall_seconds"]) for r in timings]),
        "cpu_seconds_total": round(sum(float(r["cpu_seconds"]) for r in timings), 6),
        "alerts": len(alerts),
    }
    return metrics, labels


def score(fixtures: Path, run_dir: Path) -> dict[str, Any]:
    """Score every arm of a run; writes scores.json and auto_labels.jsonl to the run."""
    run_file = run_dir / "run.json"
    if not run_file.is_file():
        raise HarnessError(f"{run_dir} has no run.json; is it a harness run?")
    run_info = json.loads(run_file.read_text(encoding="utf-8"))
    instances, clean = load_truth(fixtures)
    if run_info["fixtures"]["answer_key_sha256"] != load_manifest(fixtures)["answer_key_sha256"]:
        raise HarnessError("this run was made on a different fixture set")
    delivered = _deliveries(run_dir)

    arms: dict[str, Any] = {}
    labels: list[dict[str, Any]] = []
    for arm in sorted(a["arm"] for a in run_info["arms"]):
        alerts = read_alerts(run_dir / "alerts" / f"{arm}.jsonl")
        if any(a.arm != arm for a in alerts):
            raise HarnessError(f"alerts/{arm}.jsonl holds alerts of another arm")
        arms[arm], arm_labels = score_arm(arm, alerts, instances, clean, delivered)
        labels += arm_labels

    scores = {
        "format": SCORES_FORMAT,
        "matching_rule": MATCHING_RULE,
        "answer_key_sha256": run_info["fixtures"]["answer_key_sha256"],
        "instances": len(instances),
        "clean_controls": len(clean),
        "arms": arms,
    }
    (run_dir / "scores.json").write_text(
        json.dumps(scores, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (run_dir / "auto_labels.jsonl").write_text(
        "".join(json.dumps(label, sort_keys=True) + "\n" for label in labels),
        encoding="utf-8",
        newline="\n",
    )
    return scores
