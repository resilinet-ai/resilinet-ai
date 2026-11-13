"""Write a SandboxBank dataset to disk with a SHA-256 manifest, and verify one.

Layout::

    <out>/accounts.csv
    <out>/merchants.csv
    <out>/transactions_raw/B000000.csv ...
    <out>/manifest.json

Files are UTF-8 CSV with a header row and ``\\n`` line endings on every platform.
The manifest holds no generation time, so the same config always produces a
byte-identical directory, manifest included.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sandboxbank import __version__
from sandboxbank.generator import (
    GeneratorConfig,
    batch_id,
    format_timestamp,
    generate_accounts,
    generate_batch,
    generate_merchants,
)
from sandboxbank.schema import SCHEMA_VERSION, column_names

MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    table: str
    rows: int
    bytes: int
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_table(
    out_dir: Path, relative: str, table: str, rows: Iterable[tuple[str, ...]]
) -> FileEntry:
    path = out_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(column_names(table))
        for row in rows:
            writer.writerow(row)
            count += 1
    return FileEntry(relative, table, count, path.stat().st_size, sha256_file(path))


def build_manifest(config: GeneratorConfig, files: list[FileEntry]) -> dict[str, Any]:
    return {
        "generator": "rai-sandboxbank",
        "generator_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "seed": config.seed,
        "seed_purpose": config.purpose,
        "config": {
            "accounts": config.accounts,
            "merchants": config.merchants,
            "batches": config.batches,
            "rows_per_batch": config.rows_per_batch,
            "batch_window_seconds": config.batch_window_seconds,
            "start": format_timestamp(config.start),
        },
        "files": [
            {
                "path": entry.path,
                "table": entry.table,
                "rows": entry.rows,
                "bytes": entry.bytes,
                "sha256": entry.sha256,
            }
            for entry in files
        ],
    }


def write_dataset(config: GeneratorConfig, out_dir: Path, *, force: bool = False) -> dict[str, Any]:
    """Generate every table for ``config`` into ``out_dir`` and return the manifest."""
    if out_dir.exists() and any(out_dir.iterdir()) and not force:
        raise FileExistsError(f"{out_dir} is not empty; pass force=True to overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    accounts = generate_accounts(config)
    merchants = generate_merchants(config)
    files = [
        _write_table(out_dir, "accounts.csv", "accounts", (a.as_row() for a in accounts)),
        _write_table(out_dir, "merchants.csv", "merchants", (m.as_row() for m in merchants)),
    ]
    for index in range(config.batches):
        batch = generate_batch(config, accounts, merchants, index)
        files.append(
            _write_table(
                out_dir,
                f"transactions_raw/{batch_id(index)}.csv",
                "transactions_raw",
                (txn.as_row() for txn in batch),
            )
        )

    manifest = build_manifest(config, files)
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def verify_dataset(out_dir: Path) -> list[str]:
    """Re-hash every file named in the manifest; return problems (empty means intact)."""
    manifest_path = out_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"{MANIFEST_NAME} not found in {out_dir}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for entry in manifest["files"]:
        path = out_dir / entry["path"]
        if not path.is_file():
            problems.append(f"{entry['path']}: missing")
        elif sha256_file(path) != entry["sha256"]:
            problems.append(f"{entry['path']}: SHA-256 does not match the manifest")
    return problems
