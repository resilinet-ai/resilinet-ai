"""Ground truth for RPO and post-recovery damage (Protocol H3's RPO half and H4; B-30).

After each injection's RAI run finishes, the runner reconciles SandboxBank **itself**:
read-only, outside every arm, from the seeding log and the databases.

* **Lost acknowledged writes (P1).** Every write the payments-api acknowledged is in the
  seeding log (``sandboxbank seed``, hash-chained, verified first). An acknowledged
  write not in the ledger is a broken promise, whatever the ledger's head says.
* **The warehouse (P2)** against the ledger's posted rows, by ``ledger_seq``:
  *missing* (posted rows the ETL should have loaded), *duplicated* (loaded twice) and
  *unexpected* (rows the ledger never posted).
* **The feature table (P3)** against the warehouse, the same three ways.

A job catching up is not damage. So *missing* counts only rows that existed upstream
when the check began, and the check rereads every ``interval`` seconds until nothing
is missing or ``within`` seconds have passed (900 s, as RAI's own checks wait).

**Damage belongs to an injection when it is new**: found now and not reported after
any earlier injection. The keys already reported are kept in a state file, so a
resumed run attributes nothing twice.

**RPO truth** for an injection is 0 s with no new lost acknowledged write; otherwise
the onset minus the earliest new lost write's acknowledgement time, never negative.
Per the author's decision of 2026-11-26, H3's RPO half scores the **loss verdict**:
RAI matches when it agrees whether anything was lost. The seconds are descriptive.
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sandboxbank.seeder import ACKED
from sandboxbank.seeder import verify as verify_acks

CHECKS = ("p2.missing", "p2.duplicated", "p2.unexpected",
          "p3.missing", "p3.duplicated", "p3.unexpected")  # fmt: skip
KINDS = ("lost_acked", *CHECKS)
WITHIN = 900.0
INTERVAL = 10.0
SAMPLE = 10
CHUNK = 5_000


class Sources(Protocol):
    """What the reconciliation reads. Every read is read-only."""

    def ledger_has(self, txn_ids: list[str]) -> set[str]:
        """Which of these transactions the ledger holds."""
        ...

    def ledger_posted(self) -> set[int]:
        """The ``seq`` of every posted ledger row."""
        ...

    def warehouse(self) -> Counter[int]:
        """``ledger_seq`` -> how many warehouse rows carry it."""
        ...

    def features(self) -> Counter[int]:
        """``ledger_seq`` -> how many feature rows carry it."""
        ...


class DbSources:
    """SandboxBank's two databases, each in a read-only session with a statement timeout."""

    OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=120000"

    def __init__(self, ledger_dsn: str, analytics_dsn: str) -> None:
        self.ledger_dsn, self.analytics_dsn = ledger_dsn, analytics_dsn

    def _rows(self, dsn: str, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=10, options=self.OPTIONS,
                             application_name="rai-bench-truth") as conn:  # fmt: skip
            return list(conn.execute(sql, params).fetchall())

    def ledger_has(self, txn_ids: list[str]) -> set[str]:
        found: set[str] = set()
        for start in range(0, len(txn_ids), CHUNK):
            chunk = txn_ids[start : start + CHUNK]
            rows = self._rows(self.ledger_dsn, "SELECT txn_id::text FROM ledger_transactions "
                              "WHERE txn_id = ANY(%s::uuid[])", (chunk,))  # fmt: skip
            found.update(r[0] for r in rows)
        return found

    def ledger_posted(self) -> set[int]:
        sql = "SELECT seq FROM ledger_transactions WHERE status = 'posted'"
        return {int(r[0]) for r in self._rows(self.ledger_dsn, sql)}

    def _counts(self, table: str) -> Counter[int]:
        sql = f"SELECT ledger_seq, count(*) FROM {table} GROUP BY ledger_seq"  # noqa: S608
        rows = self._rows(self.analytics_dsn, sql)  # ``table`` is one of two fixed names
        return Counter({int(seq): int(n) for seq, n in rows})

    def warehouse(self) -> Counter[int]:
        return self._counts("warehouse.fact_transaction")

    def features(self) -> Counter[int]:
        return self._counts("features.fraud_features")


def acknowledged(path: Path) -> list[tuple[str, datetime]]:
    """(txn_id, acked_at) of every acknowledged write in an intact seeding log."""
    verify_acks(path)  # ValueError if edited or truncated
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [(r["txn_id"], datetime.fromisoformat(r["acked_at"].replace("Z", "+00:00")))
            for r in rows if r["outcome"] in ACKED]  # fmt: skip


def compare(upstream: Iterable[int], downstream: Counter[int], *, due: int) -> dict[str, set[int]]:
    """Missing (upstream rows up to ``due`` not downstream), duplicated, unexpected."""
    have = set(upstream)
    return {
        "missing": {s for s in have if s <= due and s not in downstream},
        "duplicated": {s for s, n in downstream.items() if n > 1},
        "unexpected": {s for s in downstream if s not in have},
    }


def damage(sources: Sources, *, p2_due: int, p3_due: int) -> dict[str, set[str]]:
    """The P2 and P3 findings now, as text keys."""
    posted, warehouse = sources.ledger_posted(), sources.warehouse()
    found: dict[str, set[str]] = {}
    stages = {"p2": (posted, warehouse, p2_due),
              "p3": (set(warehouse), sources.features(), p3_due)}  # fmt: skip
    for stage, (up, down, due) in stages.items():
        for kind, keys in compare(up, down, due=due).items():
            found[f"{stage}.{kind}"] = {str(k) for k in keys}
    return found


class Reconciler:
    """The ground-truth check the runner calls after each injection's run."""

    def __init__(
        self, sources: Sources, acks: Path, state: Path, *,
        within: float = WITHIN, interval: float = INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:  # fmt: skip
        self.sources, self.acks, self.state = sources, acks, state
        self.within, self.interval = within, interval
        self._sleep, self._monotonic, self._now = sleep, monotonic, now
        loaded = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {}
        self.reported: dict[str, set[str]] = {k: set(loaded.get(k, [])) for k in KINDS}

    def __call__(self, row: dict[str, Any]) -> dict[str, Any]:
        started = self._monotonic()
        p2_due = max(self.sources.ledger_posted(), default=0)
        p3_due = max(self.sources.warehouse(), default=0)
        while True:
            found = damage(self.sources, p2_due=p2_due, p3_due=p3_due)
            waiting = found["p2.missing"] or found["p3.missing"]
            if not waiting or self._monotonic() - started >= self.within:
                break
            self._sleep(self.interval)
        acked = acknowledged(self.acks)
        present = self.sources.ledger_has([t for t, _ in acked])
        lost_at = {t: at for t, at in acked if t not in present}
        found["lost_acked"] = set(lost_at)
        new = {k: found[k] - self.reported[k] for k in KINDS}
        for k in KINDS:
            self.reported[k] |= found[k]
        self._save()
        onset = row.get("onset_at")
        earliest = min((lost_at[t] for t in new["lost_acked"]), default=None)
        rpo: float | None = 0.0
        if earliest is not None:  # without RAI's onset the loss cannot be dated
            start = datetime.fromisoformat(str(onset).replace("Z", "+00:00")) if onset else None
            rpo = None if start is None else round(max(0.0, (start - earliest).total_seconds()), 3)
        return {
            "checked_at": self._now().isoformat(),
            "waited_seconds": round(self._monotonic() - started, 3),
            "acknowledged": len(acked),
            "new": {k: len(v) for k, v in new.items()},
            "sample": {k: sorted(v)[:SAMPLE] for k, v in new.items() if v},
            "earliest_lost_acked_at": None if earliest is None else earliest.isoformat(),
            "rpo_truth_seconds": rpo,
            "lost": bool(new["lost_acked"]),
            "damaged": any(new[k] for k in KINDS),
        }

    def _save(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        data = {k: sorted(v) for k, v in self.reported.items()}
        self.state.write_text(json.dumps(data) + "\n", encoding="utf-8")
