"""Run the arms over a sealed fixture set (Protocol sections 2, 8 and 9; B-15).

The runner plays the pipeline: it delivers each batch of ``feed/`` in order to
every arm, records when it delivered it, how long the arm took (wall and CPU
seconds) and the alerts the arm raised. It never opens ``answer_key/``; only the
scorer does, after the run.

Output folder::

    run.json             fixtures (seeds, answer-key hash), arms, environment
    deliveries.jsonl     one line per arm and batch: delivered, finished, timings
    alerts/<arm>.jsonl   the arm's alerts in the common format
    state/<arm>/         the arm's own working state (e.g. RAI's key history)
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rai_bench.alerts import Alert, format_time, write_alerts
from rai_bench.arms import Arm, Delivery
from rai_bench.environment import environment
from sandboxbank.generator import GeneratorConfig

RUN_FORMAT = "rai-bench-run/1"
PROTOCOL = "v0.1 draft (frozen as v1.0 on day 60)"
_BATCH_FILE = re.compile(r"^B(\d{6})\.csv$")


class HarnessError(Exception):
    """The run cannot start or its inputs are not trustworthy (CLI exit code 2)."""


@dataclass(frozen=True, slots=True)
class RunSummary:
    out: Path
    batches: int
    alerts: dict[str, int]


def load_manifest(fixtures: Path) -> dict[str, Any]:
    path = fixtures / "manifest.json"
    if not path.is_file():
        raise HarnessError(f"{fixtures} has no manifest.json; is it a SandboxBank fixture set?")
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return manifest


def _check_feed(fixtures: Path, manifest: dict[str, Any]) -> None:
    """Every delivered file must match its hash in the manifest."""
    for name, digest in sorted(manifest.get("files", {}).items()):
        if not name.startswith("feed/"):
            continue  # the answer key is the scorer's business
        path = fixtures / name
        if not path.is_file():
            raise HarnessError(f"{name} is listed in the manifest but missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise HarnessError(f"{name} does not match its manifest hash")


def deliveries(fixtures: Path, manifest: dict[str, Any]) -> list[Delivery]:
    """The batches in delivery order, each with its arrival time on SandboxBank's
    public schedule (the end of the batch's window)."""
    feed = fixtures / "feed"
    schedule = GeneratorConfig(seed=int(manifest["data_seed"]))
    found: list[Delivery] = []
    for path in sorted((feed / "transactions_raw").glob("B*.csv")):
        match = _BATCH_FILE.match(path.name)
        if match is None:
            continue
        index = int(match.group(1))
        dimension = feed / "merchants_by_batch" / path.name
        found.append(
            Delivery(
                batch_id=path.stem,
                file=path,
                feed=feed,
                arrival=schedule.batch_start(index + 1),
                dimension=dimension if dimension.is_file() else None,
            )
        )
    if not found:
        raise HarnessError(f"no batches in {feed / 'transactions_raw'}")
    return found


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run(
    fixtures: Path,
    out: Path,
    arms: Sequence[Arm],
    *,
    allow_evaluation: bool = False,
    repo_root: Path | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> RunSummary:
    """Deliver every batch to every arm and record what they raise."""
    manifest = load_manifest(fixtures)
    purpose = manifest.get("seed_purpose")
    if purpose == "evaluation" and not allow_evaluation:
        raise HarnessError(
            "these are evaluation fixtures (seeds 100,000-199,999); they may only be run "
            "for the registered benchmark, with --allow-evaluation"
        )
    if purpose not in ("development", "evaluation"):
        raise HarnessError(f"unknown seed purpose {purpose!r} in manifest.json")
    names = [arm.name for arm in arms]
    if not names or len(set(names)) != len(names):
        raise HarnessError("give at least one arm, each at most once")
    if out.exists() and any(out.iterdir()):
        raise HarnessError(f"{out} is not empty; results are never overwritten")
    _check_feed(fixtures, manifest)
    batches = deliveries(fixtures, manifest)

    started = clock()
    alerts: dict[str, list[Alert]] = {name: [] for name in names}
    records: list[str] = []
    for arm in arms:
        state = out / "state" / arm.name
        state.mkdir(parents=True, exist_ok=True)
        arm.open(state, fixtures / "feed")
    try:
        for delivery in batches:
            for arm in arms:  # same batch to every arm before the next batch
                delivered = clock()
                wall, cpu = time.perf_counter(), time.process_time()
                raised = arm.observe(delivery, clock)
                wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
                finished = clock()
                alerts[arm.name] += raised
                record = {
                    "arm": arm.name,
                    "batch_id": delivery.batch_id,
                    "delivered_at": format_time(delivered),
                    "finished_at": format_time(finished),
                    "wall_seconds": round(wall, 6),
                    "cpu_seconds": round(cpu, 6),
                    "alerts": len(raised),
                }
                records.append(json.dumps(record, sort_keys=True))
    finally:
        for arm in arms:
            arm.close()

    for name, raised in alerts.items():
        write_alerts(out / "alerts" / f"{name}.jsonl", raised)
    (out / "deliveries.jsonl").write_text(
        "".join(f"{line}\n" for line in records), encoding="utf-8", newline="\n"
    )
    summary = {
        "format": RUN_FORMAT,
        "protocol": PROTOCOL,
        "started_at": format_time(started),
        "finished_at": format_time(clock()),
        "fixtures": {
            "name": fixtures.name,
            "pipeline": manifest.get("pipeline"),
            "seed_purpose": purpose,
            "data_seed": manifest.get("data_seed"),
            "injector_seed": manifest.get("injector_seed"),
            "answer_key_sha256": manifest.get("answer_key_sha256"),
            "batches": len(batches),
        },
        "arms": [arm.describe() for arm in arms],
        "environment": environment(repo_root or Path.cwd()),
    }
    (out / "run.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return RunSummary(out, len(batches), {name: len(a) for name, a in alerts.items()})
