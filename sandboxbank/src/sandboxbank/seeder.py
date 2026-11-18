"""The seeding log: RPO ground truth from outside the system under test (B-30; threat T-07).

``sandboxbank seed`` posts transactions to the payments-api one at a time, at a steady
rate, while a chaos scenario runs. Every attempt is appended to a CSV log on the
seeder's own disk, **after** the API answered and **before** the next one is sent:

=========  ==================================================================
n          1, 2, 3, ... (the order of the attempts)
txn_id     the transaction posted
outcome    created, replayed, conflict, rejected, or unacknowledged (no 2xx/4xx
           answer: a timeout, a refused connection or a 5xx)
acked      ``true`` when the API acknowledged the write (created or replayed)
seq        the ledger ``seq`` the API returned, if any
acked_at   when the answer arrived (UTC, ISO 8601)
chain      SHA-256 over the previous line's chain and this line's fields
=========  ==================================================================

An acknowledged write is a promise: the ledger said it was committed. Reconciling the
acknowledged rows against the ledger after recovery (a RAI ``consistency_check``
with ``rpo``) finds the promises that were broken, whatever the ledger's own head
says. The hash chain makes an edited or truncated log detectable
(``sandboxbank verify-acks``); the log never leaves the seeder's side.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sandboxbank.loader import to_payload

FIELDS = ("n", "txn_id", "outcome", "acked", "seq", "acked_at", "chain")
GENESIS = "0" * 64
ACKED = frozenset({"created", "replayed"})
OUTCOME_FOR_STATUS = {201: "created", 200: "replayed", 409: "conflict", 422: "rejected"}


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def link(previous: str, fields: tuple[str, ...]) -> str:
    return hashlib.sha256((previous + "\n" + "|".join(fields)).encode()).hexdigest()


@dataclass(frozen=True)
class Attempt:
    txn_id: str
    outcome: str
    seq: int | None
    acked_at: datetime

    @property
    def acked(self) -> bool:
        return self.outcome in ACKED


class AckLog:
    """Append-only CSV with a hash chain; reopening continues the chain (a restarted
    seeder keeps one log)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.count, self.head = verify(path) if path.is_file() else (0, GENESIS)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            with path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle, lineterminator="\n").writerow(FIELDS)

    def append(self, attempt: Attempt) -> None:
        fields = (
            str(self.count + 1), attempt.txn_id, attempt.outcome,
            "true" if attempt.acked else "false",
            "" if attempt.seq is None else str(attempt.seq), stamp(attempt.acked_at),
        )  # fmt: skip
        chain = link(self.head, fields)
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\n").writerow((*fields, chain))
            handle.flush()
            os.fsync(handle.fileno())  # on disk before the next write is sent
        self.count, self.head = self.count + 1, chain


def verify(path: Path) -> tuple[int, str]:
    """(rows, chain head) of an intact log; ValueError at the first broken line."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        if tuple(next(reader, ())) != FIELDS:
            raise ValueError(f"{path} is not a seeding log (unexpected header)")
        head, count = GENESIS, 0
        for count, row in enumerate(reader, start=1):
            if len(row) != len(FIELDS) or row[0] != str(count):
                raise ValueError(f"line {count + 1}: missing, reordered or malformed row")
            if link(head, tuple(row[:-1])) != row[-1]:
                raise ValueError(f"line {count + 1}: the hash chain is broken")
            head = row[-1]
    return count, head


Post = Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any] | None]]


def post_one(url: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any] | None]:
    """Status and JSON answer; status 0 when nothing usable came back."""
    request = urllib.request.Request(  # noqa: S310 - scheme checked in seed()
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, None


def seed(
    api_url: str,
    rows: Iterable[dict[str, str]],
    log: AckLog,
    *,
    rate: float = 10.0,
    timeout: float = 5.0,
    post: Post = post_one,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> Counter[str]:
    """Post each row once, ``rate`` per second, logging every attempt; outcome counts."""
    if urlsplit(api_url).scheme not in {"http", "https"}:
        raise ValueError("api_url must be an http or https URL")
    if not 0 < rate <= 1000:
        raise ValueError("rate must be more than 0 and at most 1000 per second")
    endpoint = api_url.rstrip("/") + "/v1/transactions"
    totals: Counter[str] = Counter()
    interval = 1.0 / rate
    for row in rows:
        started = time.monotonic()
        status, answer = post(endpoint, to_payload(row), timeout)
        outcome = OUTCOME_FOR_STATUS.get(status, "unacknowledged")
        seq = answer.get("seq") if isinstance(answer, dict) else None
        if outcome == "unacknowledged" or not isinstance(seq, int):
            seq = None
        log.append(Attempt(row["txn_id"], outcome, seq, clock()))
        totals[outcome] += 1
        left = interval - (time.monotonic() - started)
        if left > 0:
            sleep(left)
    return totals
