"""Known-defect fixture sets with a sealed answer key (B-07, Protocol sections 4 and 8).

Layout::

    <out>/feed/accounts.csv, merchants.csv      reference data, clean
    <out>/feed/transactions_raw/B000000.csv ... one file per batch, some defective
    <out>/feed/merchants_by_batch/Bnnnnnn.csv   D12 only: the dimension sent with that batch
    <out>/answer_key/ground_truth.jsonl         one line per batch; clean batches included
    <out>/answer_key/SEAL                        SHA-256 of ground_truth.jsonl
    <out>/manifest.json                          seeds, plan and the hash of every file

Arms under test read only ``feed/``. The answer key is opened only for scoring;
committing the SEAL hash publicly before the runs proves it was not changed later.

Two seeds, two independent streams: ``data_seed`` drives the generator and
``injector_seed`` decides which batch gets which defect and where. Both must be
in the same protocol range, so development fixtures can never use evaluation
seeds (Protocol: development 0-99,999, evaluation 100,000-199,999).
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sandboxbank import __version__
from sandboxbank.dataset import sha256_file
from sandboxbank.generator import (
    GeneratorConfig,
    batch_id,
    format_timestamp,
    generate_accounts,
    generate_batch,
    generate_merchants,
    seed_purpose,
    stream,
)
from sandboxbank.injector import CLASS_NAMES, RATES, Context, inject
from sandboxbank.schema import column_names

PROTOCOL_BATCHES_PER_CLASS = 12
PROTOCOL_CLEAN_BATCHES = 36


@dataclass(frozen=True, slots=True)
class PlannedBatch:
    index: int
    defect_class: str | None
    rate: float | None


def plan(
    injector_seed: int,
    *,
    batches_per_class: int = PROTOCOL_BATCHES_PER_CLASS,
    clean: int = PROTOCOL_CLEAN_BATCHES,
) -> list[PlannedBatch]:
    """Which batch gets which defect class and rate, in a seeded random order.

    Rates cycle evenly within a class (12 batches over 3 rates = 4 each). Batch 0 is
    always clean, because D02 and D11 need an earlier delivered batch.
    """
    if batches_per_class < 1 or clean < 1:
        raise ValueError("batches_per_class and clean must be at least 1")
    slots: list[tuple[str | None, float | None]] = [(None, None)] * clean
    for defect_class, rates in RATES.items():
        slots += [(defect_class, rates[i % len(rates)]) for i in range(batches_per_class)]
    rng = stream(injector_seed, "injector/plan")
    rng.shuffle(slots)
    first_clean = next(i for i, (defect_class, _) in enumerate(slots) if defect_class is None)
    slots[0], slots[first_clean] = slots[first_clean], slots[0]
    return [PlannedBatch(i, c, r) for i, (c, r) in enumerate(slots)]


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_fixtures(
    out_dir: Path,
    *,
    data_seed: int,
    injector_seed: int,
    accounts: int = 5_000,
    merchants: int = 500,
    rows_per_batch: int = 100_000,
    batches_per_class: int = PROTOCOL_BATCHES_PER_CLASS,
    clean: int = PROTOCOL_CLEAN_BATCHES,
    force: bool = False,
) -> dict[str, Any]:
    """Write a fixture set and return its manifest. Same inputs give identical bytes."""
    purpose = seed_purpose(data_seed)
    if seed_purpose(injector_seed) != purpose:
        raise ValueError("data_seed and injector_seed must be in the same protocol range")
    if data_seed == injector_seed:
        raise ValueError("data_seed and injector_seed must differ")
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise FileExistsError(f"{out_dir} is not empty; pass force=True to overwrite")

    batches = plan(injector_seed, batches_per_class=batches_per_class, clean=clean)
    config = GeneratorConfig(
        seed=data_seed,
        accounts=accounts,
        merchants=merchants,
        batches=len(batches),
        rows_per_batch=rows_per_batch,
    )
    account_rows = generate_accounts(config)
    merchant_rows = generate_merchants(config)
    limits = {a.account_id: Decimal(a.txn_limit_cents).scaleb(-2) for a in account_rows}
    txn_header = list(column_names("transactions_raw"))
    merchants_header = list(column_names("merchants"))
    merchants_table = [list(m.as_row()) for m in merchant_rows]

    feed = out_dir / "feed"
    _write_csv(
        feed / "accounts.csv",
        list(column_names("accounts")),
        [list(a.as_row()) for a in account_rows],
    )
    _write_csv(feed / "merchants.csv", merchants_header, merchants_table)

    truth_lines: list[str] = []
    previous_header: list[str] = txn_header
    previous_rows: list[list[str]] = []
    reemit_pool: list[list[str]] = []
    for planned in batches:
        clean_rows = [
            list(t.as_row())
            for t in generate_batch(config, account_rows, merchant_rows, planned.index)
        ]
        name = batch_id(planned.index)
        window_start = config.batch_start(planned.index)
        record: dict[str, Any] = {
            "batch_id": name,
            "file": f"feed/transactions_raw/{name}.csv",
            "defect_class": planned.defect_class,
            "class_name": CLASS_NAMES.get(planned.defect_class or ""),
            "rate": planned.rate,
            "window_start": format_timestamp(window_start),
            # Onset = batch arrival, the end of its window (Protocol section 6).
            "onset": format_timestamp(config.batch_start(planned.index + 1)),
        }
        if planned.defect_class is None:
            header, rows = txn_header, clean_rows
            record |= {"rows": [], "columns": [], "keys": [], "whole_batch": False, "detail": {}}
        else:
            rng = stream(injector_seed, f"injector/{planned.index}")
            ctx = Context(
                window_start,
                previous_header,
                previous_rows,
                limits,
                merchants_header,
                merchants_table,
                reemit_rows=reemit_pool,
            )
            result = inject(planned.defect_class, rng, txn_header, clean_rows, planned.rate, ctx)
            header, rows = result.header, result.rows
            record |= {
                "rows": result.rows_affected,
                "columns": result.columns,
                "keys": result.keys,
                "whole_batch": result.whole_batch,
                "detail": result.detail,
            }
            if result.merchants_rows is not None:
                dimension = f"feed/merchants_by_batch/{name}.csv"
                _write_csv(out_dir / dimension, merchants_header, result.merchants_rows)
                record["dimension_file"] = dimension
        _write_csv(out_dir / record["file"], header, rows)
        truth_lines.append(_dump(record))
        # What the arms actually received is the "previous batch" for D11, and its
        # unaffected rows (standard columns, values untouched) are what D02 re-emits.
        previous_header, previous_rows = header, rows
        if header == txn_header and planned.defect_class != "D09":
            affected = set(record["rows"])
            reemit_pool = [row for i, row in enumerate(rows) if i not in affected]

    answer_key = out_dir / "answer_key" / "ground_truth.jsonl"
    answer_key.parent.mkdir(parents=True, exist_ok=True)
    answer_key.write_text("\n".join(truth_lines) + "\n", encoding="utf-8", newline="\n")
    seal = sha256_file(answer_key)
    (out_dir / "answer_key" / "SEAL").write_text(seal + "\n", encoding="utf-8", newline="\n")

    files = sorted(
        p.relative_to(out_dir).as_posix()
        for p in out_dir.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    )
    manifest = {
        "generator": "rai-sandboxbank",
        "generator_version": __version__,
        "kind": "known-defect fixtures",
        "pipeline": "P1",
        "synthetic": True,
        "seed_purpose": purpose,
        "data_seed": data_seed,
        "injector_seed": injector_seed,
        "config": {
            "accounts": accounts,
            "merchants": merchants,
            "rows_per_batch": rows_per_batch,
            "batches_per_class": batches_per_class,
            "clean_batches": clean,
            "batches": len(batches),
        },
        "answer_key_sha256": seal,
        "files": {path: sha256_file(out_dir / path) for path in files},
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def verify_seal(out_dir: Path, expected: str | None = None) -> bool:
    """True if the answer key still matches its SEAL (and ``expected``, when given)."""
    answer_key = out_dir / "answer_key" / "ground_truth.jsonl"
    seal_file = out_dir / "answer_key" / "SEAL"
    if not answer_key.is_file() or not seal_file.is_file():
        return False
    actual = hashlib.sha256(answer_key.read_bytes()).hexdigest()
    sealed = seal_file.read_text(encoding="utf-8").strip()
    return actual == sealed and (expected is None or actual == expected)


SLICE_DEFECT = "D02"
SLICE_RATE = 0.001


def build_slice(
    out_dir: Path,
    *,
    data_seed: int,
    injector_seed: int,
    accounts: int = 5_000,
    merchants: int = 500,
    rows_per_batch: int = 100_000,
    force: bool = False,
) -> dict[str, Any]:
    """The first runnable slice (Build Plan section 5): one clean batch, then one batch
    that re-sends 0.1% of the first batch's rows (D02: 100 of 100,000).

    Same layout as a fixture set (``feed/``, ``answer_key/``), with two batches.
    """
    if seed_purpose(data_seed) != seed_purpose(injector_seed):
        raise ValueError("data_seed and injector_seed must be in the same protocol range")
    if data_seed == injector_seed:
        raise ValueError("data_seed and injector_seed must differ")
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise FileExistsError(f"{out_dir} is not empty; pass force=True to overwrite")
    if force and out_dir.exists():
        for path in sorted(out_dir.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()

    config = GeneratorConfig(
        seed=data_seed,
        accounts=accounts,
        merchants=merchants,
        batches=2,
        rows_per_batch=rows_per_batch,
    )
    account_rows = generate_accounts(config)
    merchant_rows = generate_merchants(config)
    txn_header = list(column_names("transactions_raw"))
    merchants_header = list(column_names("merchants"))
    merchants_table = [list(m.as_row()) for m in merchant_rows]
    feed = out_dir / "feed"
    _write_csv(
        feed / "accounts.csv",
        list(column_names("accounts")),
        [list(a.as_row()) for a in account_rows],
    )
    _write_csv(feed / "merchants.csv", merchants_header, merchants_table)

    clean = [list(t.as_row()) for t in generate_batch(config, account_rows, merchant_rows, 0)]
    second = [list(t.as_row()) for t in generate_batch(config, account_rows, merchant_rows, 1)]
    limits = {a.account_id: Decimal(a.txn_limit_cents).scaleb(-2) for a in account_rows}
    ctx = Context(
        config.batch_start(1),
        txn_header,
        clean,
        limits,
        merchants_header,
        merchants_table,
        reemit_rows=clean,
    )
    result = inject(
        SLICE_DEFECT, stream(injector_seed, "injector/slice"), txn_header, second, SLICE_RATE, ctx
    )
    truth = []
    for index, (rows, defect) in enumerate(((clean, None), (result.rows, SLICE_DEFECT))):
        name = batch_id(index)
        _write_csv(feed / "transactions_raw" / f"{name}.csv", txn_header, rows)
        truth.append(
            {
                "batch_id": name,
                "file": f"feed/transactions_raw/{name}.csv",
                "defect_class": defect,
                "class_name": CLASS_NAMES.get(defect or ""),
                "rate": SLICE_RATE if defect else None,
                "rows": result.rows_affected if defect else [],
                "columns": result.columns if defect else [],
                "keys": result.keys if defect else [],
                "window_start": format_timestamp(config.batch_start(index)),
                "onset": format_timestamp(config.batch_start(index + 1)),
            }
        )
    answer_key = out_dir / "answer_key" / "ground_truth.jsonl"
    answer_key.parent.mkdir(parents=True, exist_ok=True)
    answer_key.write_text("\n".join(_dump(t) for t in truth) + "\n", encoding="utf-8", newline="\n")
    seal = sha256_file(answer_key)
    (out_dir / "answer_key" / "SEAL").write_text(seal + "\n", encoding="utf-8", newline="\n")
    return {"answer_key_sha256": seal, "batches": truth, "seed_purpose": seed_purpose(data_seed)}
