"""Cross-batch history at full protocol size (NFR-PERF-1, decision RAI-DUPES; B-15).

A pipeline in the benchmark delivers 180 batches of 100,000 rows, so by its last
batch the cross-batch duplicate check looks keys up in a history of about 18M.
This check fills an SQLite key history with that many random UUIDv4 keys (the
shape of SandboxBank's txn_id), plants a known number of the batch's own keys in
it, then validates a real 100,000-row batch against it several times. The planted
keys must all be found, and the time is compared with the 10 s budget.
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rai.validation.contract import load_contract
from rai.validation.engine import read_batch, validate
from rai.validation.history import SqliteKeyStore
from rai_bench.runner import HarnessError
from rai_bench.scoring import percentile
from sandboxbank.fixtures import build_slice

RULE_ID = "unique_across_batches:txn_id"
BUDGET_SECONDS = 10.0
CHUNK = 100_000


def history_scale(
    out: Path,
    contracts: Path,
    *,
    keys: int = 18_000_000,
    planted: int = 100,
    repeats: int = 5,
    rows: int = 100_000,
    seed: int = 9_000,
) -> dict[str, Any]:
    if out.exists() and any(out.iterdir()):
        raise HarnessError(f"{out} is not empty")
    if not 0 <= planted <= min(keys, rows) or repeats < 1:
        raise HarnessError("planted must be between 0 and both keys and rows; repeats >= 1")
    build_slice(out / "slice", data_seed=seed, injector_seed=seed + 1, rows_per_batch=rows)
    feed = out / "slice" / "feed"
    batch = read_batch(feed / "transactions_raw" / "B000000.csv")
    contract = load_contract(contracts / "transactions_raw.yaml")
    references = {
        "accounts": read_batch(feed / "accounts.csv"),
        "merchants": read_batch(feed / "merchants.csv"),
    }
    scope = f"{contract.contract}:{RULE_ID}"
    plant = sorted(batch.get_column("txn_id").to_list())[:planted]

    rng = random.Random(seed)  # noqa: S311 - synthetic keys, not security
    started = time.perf_counter()
    with SqliteKeyStore(out / "history.sqlite") as history:
        written = history.record(scope, plant, "H-planted") if plant else 0
        while written < keys:
            size = min(CHUNK, keys - written)
            chunk = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(size)]
            written += history.record(scope, chunk, f"H{written // CHUNK:06d}")
        fill_seconds = time.perf_counter() - started

        times: list[float] = []
        found = -1
        for _ in range(repeats):
            started = time.perf_counter()
            report = validate(
                contract,
                batch,
                references=references,
                as_of=datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
                history=history,
                batch_id="B000000",
                record=False,
            )
            times.append(time.perf_counter() - started)
            found = next(r.failing_count for r in report.results if r.rule_id == RULE_ID)
    worst = max(times)
    return {
        "history_keys": written,
        "history_bytes": sum(p.stat().st_size for p in out.glob("history.sqlite*")),
        "fill_seconds": round(fill_seconds, 3),
        "batch_rows": batch.height,
        "rules": len(report.results),
        "planted": planted,
        "found": found,
        "validate_seconds": [round(t, 3) for t in times],
        "validate_p50_seconds": round(percentile(times, 0.5) or 0.0, 3),
        "validate_max_seconds": round(worst, 3),
        "budget_seconds": BUDGET_SECONDS,
        "within_budget": worst <= BUDGET_SECONDS and found == planted,
    }
