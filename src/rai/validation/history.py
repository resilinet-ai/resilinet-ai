"""Batch key history for cross-batch duplicate detection (B-10, FR-03.3).

A key store remembers, per scope (contract and rule), every key value it has
seen and the batch that first delivered it. Looking a batch up never changes the
store; recording it does, and only the first sighting of a key is kept. Keys
first seen in the *same* batch id are not duplicates of themselves, so
validating a batch again gives the same result ("repeatable output").

``MemoryKeyStore`` suits tests and one-off runs. ``SqliteKeyStore`` persists to a
single file using only the standard library; a protocol-size run (180 batches of
100,000 rows) is about 18 million keys, which is too many to hold in memory
comfortably.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol


class KeyStore(Protocol):
    def seen_elsewhere(self, scope: str, keys: Iterable[str], batch_id: str | None) -> set[str]:
        """Keys already recorded from a batch other than ``batch_id``."""
        ...

    def record(self, scope: str, keys: Iterable[str], batch_id: str) -> int:
        """Remember keys as delivered by ``batch_id``; return how many were new."""
        ...


class MemoryKeyStore:
    def __init__(self) -> None:
        self._first_seen: dict[str, dict[str, str]] = {}

    def seen_elsewhere(self, scope: str, keys: Iterable[str], batch_id: str | None) -> set[str]:
        known = self._first_seen.get(scope, {})
        return {key for key in keys if key in known and known[key] != batch_id}

    def record(self, scope: str, keys: Iterable[str], batch_id: str) -> int:
        known = self._first_seen.setdefault(scope, {})
        before = len(known)
        for key in keys:
            known.setdefault(key, batch_id)
        return len(known) - before


class SqliteKeyStore:
    """File-backed key store; one table, primary key (scope, key)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        # WAL with synchronous=NORMAL survives a process crash (not a power cut mid-commit),
        # which is right for a re-buildable cache of delivered keys.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA cache_size=-262144")  # 256 MiB page cache
        self._db.execute("PRAGMA temp_store=MEMORY")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            " scope TEXT NOT NULL, key TEXT NOT NULL, batch_id TEXT NOT NULL,"
            " PRIMARY KEY (scope, key)) WITHOUT ROWID"
        )
        self._db.commit()

    def seen_elsewhere(self, scope: str, keys: Iterable[str], batch_id: str | None) -> set[str]:
        cursor = self._db.cursor()
        cursor.execute("CREATE TEMP TABLE IF NOT EXISTS probe (key TEXT PRIMARY KEY)")
        cursor.execute("DELETE FROM probe")
        # Sorted keys walk the B-tree in order: far fewer page reads than random UUIDs.
        cursor.executemany("INSERT OR IGNORE INTO probe VALUES (?)", ((k,) for k in sorted(keys)))
        rows = cursor.execute(
            "SELECT p.key FROM probe p JOIN seen s ON s.scope = ? AND s.key = p.key "
            "WHERE s.batch_id IS NOT ?",
            (scope, batch_id),
        ).fetchall()
        cursor.execute("DELETE FROM probe")
        return {row[0] for row in rows}

    def record(self, scope: str, keys: Iterable[str], batch_id: str) -> int:
        with self._db:
            before = self._db.total_changes
            self._db.executemany(
                "INSERT OR IGNORE INTO seen (scope, key, batch_id) VALUES (?, ?, ?)",
                ((scope, key, batch_id) for key in sorted(keys)),
            )
            return self._db.total_changes - before

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SqliteKeyStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
