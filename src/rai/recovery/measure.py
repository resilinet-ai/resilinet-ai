"""RTO and RPO from what the worker observed (B-26b: FR-08.1, FR-08.2). Pure functions.

**RTO** (FR-08.1) runs from the fault's onset (t0, the agent's record) to *validated
restoration*: the first moment after the last fault was reverted from which every
probe answered healthy ``stable`` times in a row. A recovery that flaps does not
count until it holds. Probes before the last revert are ignored: the system cannot
be restored while a fault is still applied.

**RPO** (FR-08.2) is measured on the ledger, which is append-only: its row count
never falls and ``seq`` only grows, unless committed data was lost. From the head
read just before the first fault (``before``) and after restoration (``after``):

* nothing lost (rows and ``seq`` did not fall): RPO is 0 s;
* the ledger went back (``after.seq < before.seq``): the recovery point is the record
  at ``after``, and RPO is ``onset - after.committed_at``;
* rows fell although ``seq`` did not (records lost and new ones written since): the
  recovery point cannot be dated from the head, so RPO is unknown, which fails any
  objective.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Sample:
    at: datetime
    healthy: bool  # every probe answered 2xx in this round


@dataclass(frozen=True)
class Rto:
    onset: datetime
    restored_at: datetime | None  # None: not restored within the window

    @property
    def seconds(self) -> float | None:
        if self.restored_at is None:
            return None
        return round((self.restored_at - self.onset).total_seconds(), 3)


def rto(onset: datetime, reverted: datetime, samples: list[Sample], stable: int) -> Rto:
    run_start: datetime | None = None
    run = 0
    for sample in sorted(samples, key=lambda s: s.at):
        if sample.at < reverted:
            continue
        if sample.healthy:
            run_start = run_start or sample.at
            run += 1
            if run >= stable:
                return Rto(onset, run_start)
        else:
            run_start, run = None, 0
    return Rto(onset, None)


@dataclass(frozen=True)
class Head:
    """The ledger head: last committed ``seq``, when it committed, and the row count."""

    seq: int
    committed_at: datetime | None
    rows: int

    @classmethod
    def parse(cls, body: Any) -> Head:
        """SandboxBank's ``GET /v1/ledger/head``; raises ValueError if it is not one."""
        if not isinstance(body, dict):
            raise ValueError("the ledger head is not a JSON object")
        try:
            committed = body.get("committed_at")
            return cls(
                seq=int(body.get("seq") or 0),
                committed_at=(
                    datetime.fromisoformat(str(committed).replace("Z", "+00:00"))
                    if committed else None
                ),
                rows=int(body["rows"]),
            )  # fmt: skip
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"the ledger head is not usable: {error}") from None


@dataclass(frozen=True)
class Rpo:
    seconds: float | None  # None: unknown (data lost, recovery point not datable)
    lost_rows: int
    recovery_point: Head | None


def rpo(onset: datetime, before: Head, after: Head) -> Rpo:
    lost = max(0, before.rows - after.rows)
    if after.seq >= before.seq and lost == 0:
        return Rpo(0.0, 0, before)
    if after.seq < before.seq:
        point = after.committed_at
        seconds = None if point is None else max(0.0, (onset - point).total_seconds())
        return Rpo(None if seconds is None else round(seconds, 3), lost, after)
    return Rpo(None, lost, None)
