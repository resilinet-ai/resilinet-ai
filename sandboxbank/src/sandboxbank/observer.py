"""The recovery observer: RTO ground truth from outside every arm (B-44; Protocol H3).

Protocol H3 asks whether RAI's RTO is within ± 2 s of the truth. The truth cannot come
from RAI's own probes, so ``sandboxbank observe`` runs in its own container, part of no
arm. Every ``interval`` seconds (250 ms by default), on a schedule kept by its own
monotonic clock, it takes one **sample**:

* **the API:** ``GET /readyz`` on the payments-api, the way clients reach it;
* **the jobs** (P2 and P3, when given): ``GET /readyz`` on the etl-job and the
  feature-job, their freshness probes (B-28), so ground truth for a P2 or P3 scenario
  sees what its recovery probes see;
* **the consistency query:** a read-only ``max(seq)`` on ledger-db, connecting
  directly rather than through Toxiproxy, so the ledger's own state is seen too.

Each check has ``timeout`` (2 s, the same as RAI's probes). Each sample is appended to a
CSV log on the observer's own disk and fsynced before the next one is taken:

=========  ==================================================================
n          1, 2, 3, ... (the order of the samples)
at         when the sample started (UTC, ISO 8601). It is the wall clock read
           once at start-up plus the monotonic time since, so a clock step
           during a run cannot move it
api        HTTP status of ``/readyz``, or 0 when it did not answer
etl        the same for the etl-job's ``/readyz``; empty when not watched
features   the same for the feature-job's ``/readyz``; empty when not watched
ledger     ``ok`` or ``error``
seq        the ledger's ``max(seq)``, if it answered
healthy    ``true`` when every watched check answered 2xx and the ledger answered
chain      SHA-256 over the previous line's chain and this line's fields
=========  ==================================================================

Logs written before the jobs were watched (no ``etl`` and ``features`` columns) are
still read and verified; the observer refuses to append to one, so a log has one format.

**Ground truth** (:func:`restoration`) applies RAI's restoration rule to these samples,
for the injection's recovery probes (``payments-health``, ``etl-ready``,
``features-ready``; :data:`PROBES`): a sample is healthy for them when each of their
checks answered 2xx and the ledger answered. Ignore samples before the last revert, then
take the first healthy sample from which the system stayed healthy for ``hold`` seconds.
RAI holds for ``stable_checks`` probe rounds 1 s apart, so its default of 3 rounds is a
hold of 2 s. Only the clock and the sampling differ, so the difference measures RAI's
measurement, not a different definition.

The log is hash-chained like the seeding log (B-30), so an edited or truncated log is
detectable (``sandboxbank verify-observations``).
"""

from __future__ import annotations

import contextlib
import csv
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sandboxbank.seeder import GENESIS, link, stamp

FIELDS = ("n", "at", "api", "etl", "features", "ledger", "seq", "healthy", "chain")
FIELDS_V1 = ("n", "at", "api", "ledger", "seq", "healthy", "chain")  # before P2 and P3
# The registry's recovery probes (B-28), each with the check of a sample that sees the same
PROBES = {"payments-health": "api", "etl-ready": "etl", "features-ready": "features"}
P1_PROBES = ("payments-health",)
INTERVAL = 0.25
TIMEOUT = 2.0
HOLD = 2.0  # RAI's default: 3 stable probe rounds, 1 s apart
MAX_GAP = 5.0  # longer between two samples and the observer was not watching


@dataclass(frozen=True)
class Sample:
    at: datetime
    api: int  # HTTP status of /readyz; 0 when it did not answer
    ledger_ok: bool
    seq: int | None
    etl: int | None = None  # the etl-job's /readyz; None when not watched
    features: int | None = None  # the feature-job's /readyz; None when not watched

    @property
    def healthy(self) -> bool:
        """Every watched check answered 2xx and the ledger answered."""
        watched = [s for s in (self.api, self.etl, self.features) if s is not None]
        return self.ledger_ok and all(200 <= s < 300 for s in watched)

    def watched(self, probe: str) -> bool:
        return getattr(self, PROBES[probe]) is not None

    def healthy_for(self, probes: tuple[str, ...]) -> bool:
        """RAI's restoration condition for these recovery probes, plus the ledger."""
        statuses = [getattr(self, PROBES[p]) for p in probes]
        return self.ledger_ok and all(s is not None and 200 <= s < 300 for s in statuses)


class ObservationLog:
    """Append-only CSV with a hash chain; reopening continues the chain."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_file() and _header(path) == FIELDS_V1:
            raise ValueError(f"{path} has the earlier format (no job columns): start a new log")
        self.count, self.head = verify(path) if path.is_file() else (0, GENESIS)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            with path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle, lineterminator="\n").writerow(FIELDS)

    def append(self, sample: Sample) -> None:
        fields = (
            str(self.count + 1), stamp(sample.at), str(sample.api),
            _status(sample.etl), _status(sample.features),
            "ok" if sample.ledger_ok else "error",
            "" if sample.seq is None else str(sample.seq),
            "true" if sample.healthy else "false",
        )  # fmt: skip
        chain = link(self.head, fields)
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\n").writerow((*fields, chain))
            handle.flush()
            os.fsync(handle.fileno())
        self.count, self.head = self.count + 1, chain


def _status(status: int | None) -> str:
    return "" if status is None else str(status)


def _header(path: Path) -> tuple[str, ...]:
    with path.open(newline="", encoding="utf-8") as handle:
        return tuple(next(csv.reader(handle), ()))


def verify(path: Path) -> tuple[int, str]:
    """(samples, chain head) of an intact log; ValueError at the first broken line."""
    count, head = 0, GENESIS
    for _, chain in _lines(path):
        count, head = count + 1, chain
    return count, head


def _lines(path: Path) -> Iterator[tuple[Sample, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = tuple(next(reader, ()))
        if header not in {FIELDS, FIELDS_V1}:
            raise ValueError(f"{path} is not an observation log (unexpected header)")
        head = GENESIS
        for count, row in enumerate(reader, start=1):
            line = count + 1
            if len(row) != len(header) or row[0] != str(count):
                raise ValueError(f"line {line}: missing, reordered or malformed row")
            if link(head, tuple(row[:-1])) != row[-1]:
                raise ValueError(f"line {line}: the hash chain is broken")
            head = row[-1]
            value = dict(zip(header, row, strict=True))
            try:
                sample = Sample(
                    at=datetime.fromisoformat(value["at"].replace("Z", "+00:00")),
                    api=int(value["api"]), ledger_ok=value["ledger"] == "ok",
                    seq=int(value["seq"]) if value["seq"] else None,
                    etl=int(value["etl"]) if value.get("etl") else None,
                    features=int(value["features"]) if value.get("features") else None,
                )  # fmt: skip
            except ValueError:
                raise ValueError(f"line {line}: unreadable values") from None
            if sample.healthy != (value["healthy"] == "true"):
                raise ValueError(f"line {line}: healthy does not follow from api and ledger")
            yield sample, row[-1]


def read(path: Path) -> list[Sample]:
    """Every sample of an intact log, in order; ValueError if it is not intact."""
    return [sample for sample, _ in _lines(path)]


# ---- taking samples -------------------------------------------------------------

Get = Callable[[str, float], int]
Ledger = Callable[[float], int | None]  # max(seq); raises when the ledger did not answer


def get_status(url: str, timeout: float) -> int:
    """HTTP status; 0 when nothing answered."""
    request = urllib.request.Request(url, headers={"User-Agent": "sandboxbank-observer"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0


class LedgerQuery:
    """The consistency query on one connection, reopened after any failure."""

    SQL = "SELECT coalesce(max(seq), 0) FROM ledger_transactions"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.conn: Any = None

    def __call__(self, timeout: float) -> int | None:
        import psycopg

        millis = max(1, int(timeout * 1000))
        try:
            if self.conn is None or self.conn.closed:
                self.conn = psycopg.connect(
                    self.dsn, autocommit=True, connect_timeout=max(2, round(timeout)),
                    options=f"-c statement_timeout={millis} -c default_transaction_read_only=on",
                    application_name="sandboxbank-observer",
                )  # fmt: skip
            row = self.conn.execute(self.SQL).fetchone()
            return None if row is None else int(row[0])
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):  # already broken; reopened next sample
                self.conn.close()
            self.conn = None


def take(
    api_url: str, ledger: Ledger, at: datetime, timeout: float, get: Get,
    etl_url: str | None = None, features_url: str | None = None,
) -> Sample:  # fmt: skip
    def ready(url: str | None) -> int | None:
        return None if url is None else get(url.rstrip("/") + "/readyz", timeout)

    status = get(api_url.rstrip("/") + "/readyz", timeout)
    etl, features = ready(etl_url), ready(features_url)
    try:
        seq, ok = ledger(timeout), True
    except Exception:
        seq, ok = None, False
    return Sample(at, status, ok, seq, etl, features)


def observe(
    api_url: str,
    ledger: Ledger,
    log: ObservationLog,
    *,
    etl_url: str | None = None,
    features_url: str | None = None,
    interval: float = INTERVAL,
    timeout: float = TIMEOUT,
    samples: int | None = None,
    get: Get = get_status,
    monotonic: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Sample every ``interval`` seconds until ``samples`` are taken (forever if None).

    Ticks are fixed on the monotonic clock, so slow samples do not shift later ones; a
    tick missed while a sample was still running is skipped, not made up."""
    for name, url in (("api_url", api_url), ("etl_url", etl_url),
                      ("features_url", features_url)):  # fmt: skip
        if url is not None and urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError(f"{name} must be an http or https URL")
    if not 0.05 <= interval <= 10:
        raise ValueError("interval must be between 0.05 and 10 seconds")
    if not 0 < timeout <= 30:
        raise ValueError("timeout must be more than 0 and at most 30 seconds")
    anchor_wall, anchor = wall(), monotonic()
    tick, taken = anchor, 0
    while samples is None or taken < samples:
        started = monotonic()
        at = anchor_wall + timedelta(seconds=started - anchor)
        log.append(take(api_url, ledger, at, timeout, get, etl_url, features_url))
        taken += 1
        now = monotonic()
        tick += interval
        if tick <= now:  # a slow sample: skip to the next tick still ahead
            tick += interval * (int((now - tick) / interval) + 1)
        sleep(tick - now)
    return taken


# ---- ground truth ---------------------------------------------------------------


@dataclass(frozen=True)
class Truth:
    """Ground-truth restoration for one injection."""

    onset: datetime
    reverted: datetime
    restored_at: datetime | None
    observed: bool  # False: the observer was not watching; the injection is excluded
    reason: str = ""

    @property
    def seconds(self) -> float | None:
        if self.restored_at is None:
            return None
        return round((self.restored_at - self.onset).total_seconds(), 3)


def restoration(
    samples: list[Sample], onset: datetime, reverted: datetime, *,
    hold: float = HOLD, window: float | None = None, max_gap: float = MAX_GAP,
    probes: tuple[str, ...] = P1_PROBES,
) -> Truth:  # fmt: skip
    """RAI's rule on the observer's samples: the first sample after the last revert
    healthy for the injection's recovery ``probes``, from which every sample stayed
    healthy for them for ``hold`` seconds.

    Not restored within ``window`` seconds of the revert: ``restored_at`` is None. The
    observer was not watching (no sample from before the revert, a gap longer than
    ``max_gap`` before restoration was seen, or a probe it did not check):
    ``observed`` is False."""
    if reverted < onset:
        raise ValueError("the revert cannot come before the onset")
    unknown = sorted(set(probes) - set(PROBES))
    if unknown or not probes:
        raise ValueError(f"no observer check for the probes {unknown or 'given'}")
    closes = None if window is None else reverted + timedelta(seconds=window)
    ordered = sorted(samples, key=lambda s: s.at)
    if not any(s.at <= reverted for s in ordered):
        return Truth(onset, reverted, None, False, "no sample at or before the revert")
    previous: datetime | None = None
    run_start: datetime | None = None
    for sample in ordered:
        if sample.at < reverted:
            previous = sample.at
            continue
        if closes is not None and sample.at > closes:
            break
        if previous is not None and (sample.at - previous).total_seconds() > max_gap:
            gap = round((sample.at - previous).total_seconds(), 3)
            return Truth(onset, reverted, None, False, f"a {gap} s gap in the samples")
        unwatched = [p for p in probes if not sample.watched(p)]
        if unwatched:
            return Truth(onset, reverted, None, False,
                         f"the observer did not check {', '.join(unwatched)}")  # fmt: skip
        previous = sample.at
        if not sample.healthy_for(probes):
            run_start = None
            continue
        run_start = run_start or sample.at
        if (sample.at - run_start).total_seconds() >= hold:
            return Truth(onset, reverted, run_start, True)
    if closes is None or previous is None or previous < closes - timedelta(seconds=max_gap):
        return Truth(onset, reverted, None, False, "the samples end before the window closes")
    return Truth(onset, reverted, None, True, "not restored within the window")
