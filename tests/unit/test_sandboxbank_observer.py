"""The recovery observer: RTO ground truth from outside every arm (B-44; Protocol H3)."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sandboxbank.cli import main
from sandboxbank.observer import (
    FIELDS,
    ObservationLog,
    Sample,
    observe,
    read,
    restoration,
    verify,
)

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def timeline(*spans: tuple[float, float, bool], step: float = 0.25) -> list[Sample]:
    """Samples every ``step`` seconds over [start, end), healthy or not."""
    out: list[Sample] = []
    for start, end, healthy in spans:
        t = start
        while t < end - 1e-9:
            out.append(Sample(at(t), 200 if healthy else 0, healthy, 1 if healthy else None))
            t += step
    return out


class Clock:
    """A monotonic clock that advances when the observer sleeps or a check takes time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.now += seconds


def test_samples_are_taken_on_a_fixed_schedule_and_logged_with_a_chain(tmp_path: Path) -> None:
    clock = Clock()
    statuses = iter([200, 0, 503, 200])
    seqs = iter([7, None, 8, 9])

    def get(url: str, timeout: float) -> int:
        assert url == "http://api/readyz"
        assert timeout == 2.0
        clock.now += 0.05  # a check takes time; the schedule does not drift
        return next(statuses)

    def ledger(timeout: float) -> int | None:
        seq = next(seqs)
        if seq is None:
            raise OSError("ledger-db did not answer")
        return seq

    log = ObservationLog(tmp_path / "obs.csv")
    taken = observe("http://api/", ledger, log, samples=4, get=get,
                    monotonic=clock.monotonic, wall=lambda: T0, sleep=clock.sleep)  # fmt: skip
    assert taken == 4
    assert clock.slept == [0.2, 0.2, 0.2, 0.2]
    samples = read(log.path)
    assert [s.at for s in samples] == [at(0), at(0.25), at(0.5), at(0.75)]
    assert [(s.api, s.ledger_ok, s.seq, s.healthy) for s in samples] == [
        (200, True, 7, True), (0, False, None, False),
        (503, True, 8, False), (200, True, 9, True),
    ]  # fmt: skip
    assert verify(log.path) == (4, log.head)
    with log.path.open(newline="", encoding="utf-8") as handle:
        assert tuple(next(csv.reader(handle))) == FIELDS


def test_a_slow_sample_skips_the_ticks_it_missed(tmp_path: Path) -> None:
    clock = Clock()

    def get(url: str, timeout: float) -> int:
        clock.now += 0.6  # longer than two intervals
        return 0

    log = ObservationLog(tmp_path / "obs.csv")
    observe("http://api", lambda t: 1, log, samples=2, get=get,
            monotonic=clock.monotonic, wall=lambda: T0, sleep=clock.sleep)  # fmt: skip
    assert clock.slept == [0.15, 0.15]  # waits for the tick at 0.75 s, then 1.5 s
    assert [s.at for s in read(log.path)] == [at(0), at(0.75)]


def test_the_observer_refuses_bad_settings(tmp_path: Path) -> None:
    log = ObservationLog(tmp_path / "obs.csv")
    with pytest.raises(ValueError, match="http or https"):
        observe("file:///etc/passwd", lambda t: 1, log, samples=1)
    with pytest.raises(ValueError, match="interval"):
        observe("http://api", lambda t: 1, log, samples=1, interval=0.01)
    with pytest.raises(ValueError, match="timeout"):
        observe("http://api", lambda t: 1, log, samples=1, timeout=0)


def test_an_edited_reordered_or_truncated_log_is_detected(tmp_path: Path) -> None:
    log = ObservationLog(tmp_path / "obs.csv")
    for sample in timeline((0, 1, True)):
        log.append(sample)
    lines = log.path.read_text(encoding="utf-8").splitlines()

    edited = tmp_path / "edited.csv"
    edited.write_text("\n".join([*lines[:2], lines[2].replace(",200,", ",503,"), *lines[3:]]))
    with pytest.raises(ValueError, match="line 3: the hash chain is broken"):
        verify(edited)
    reordered = tmp_path / "reordered.csv"
    reordered.write_text("\n".join([lines[0], lines[2], lines[1], *lines[3:]]))
    with pytest.raises(ValueError, match="line 2: missing, reordered"):
        verify(reordered)
    forged = tmp_path / "forged.csv"  # healthy=true on an unhealthy sample, chain recomputed
    forged_log = ObservationLog(forged)
    forged_log.append(Sample(at(0), 0, True, 1))
    text = forged.read_text(encoding="utf-8")
    from sandboxbank.seeder import GENESIS, link

    fields = text.splitlines()[1].split(",")[:-1]
    fields[5] = "true"
    forged.write_text(f"{','.join(FIELDS)}\n{','.join([*fields, link(GENESIS, tuple(fields))])}\n")
    with pytest.raises(ValueError, match="healthy does not follow"):
        verify(forged)
    # Truncation: the chain head no longer matches the one published at the end of a run.
    truncated = tmp_path / "truncated.csv"
    truncated.write_text("\n".join(lines[:-1]) + "\n")
    assert verify(truncated)[1] != log.head


def test_a_restarted_observer_continues_the_same_chain(tmp_path: Path) -> None:
    path = tmp_path / "obs.csv"
    first = ObservationLog(path)
    first.append(timeline((0, 0.25, True))[0])
    second = ObservationLog(path)
    assert (second.count, second.head) == (first.count, first.head)
    second.append(timeline((5, 5.25, False))[0])
    assert verify(path) == (2, second.head)


def test_ground_truth_follows_rais_restoration_rule() -> None:
    # Fault from 10 s; reverted at 70 s; flaps once, then healthy from 72.5 s.
    samples = timeline((0, 10, True), (10, 71, False), (71, 71.5, True), (71.5, 72.5, False),
                       (72.5, 90, True))  # fmt: skip
    truth = restoration(samples, at(10), at(70))
    assert truth.observed
    assert truth.restored_at == at(72.5)
    assert truth.seconds == 62.5
    # A longer hold does not move the start of the stable run.
    assert restoration(samples, at(10), at(70), hold=5).seconds == 62.5
    # Healthy samples before the revert never count, even if the probe passes.
    early = timeline((0, 10, True), (10, 30, False), (30, 90, True))
    assert restoration(early, at(10), at(70)).seconds == 60.0


def test_not_restored_and_unobserved_are_different_answers() -> None:
    down = timeline((0, 200, False))
    assert restoration(down, at(10), at(70), window=60) == restoration(
        down, at(10), at(70), window=60
    )
    missed = restoration(down, at(10), at(70), window=60)
    assert missed.observed
    assert missed.restored_at is None
    assert missed.seconds is None
    # The observer started after the revert: it cannot say when recovery began.
    late = restoration(timeline((80, 120, True)), at(10), at(70))
    assert not late.observed
    assert "before the revert" in late.reason
    # A gap in the samples while the system was down: unobserved, not a value.
    gap = timeline((0, 72, False), (80, 90, True))
    gapped = restoration(gap, at(10), at(70))
    assert not gapped.observed
    assert "gap" in gapped.reason
    # The log ends before the window closes, still down: unobserved.
    short = restoration(timeline((0, 75, False)), at(10), at(70), window=60)
    assert not short.observed
    with pytest.raises(ValueError, match="before the onset"):
        restoration(down, at(70), at(10))


def test_the_cli_verifies_a_log_and_prints_the_ground_truth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = ObservationLog(tmp_path / "obs.csv")
    for sample in timeline((0, 10, True), (10, 72, False), (72, 80, True)):
        log.append(sample)
    assert main(["verify-observations", str(log.path)]) == 0
    assert f"intact: {log.count} samples" in capsys.readouterr().out
    onset, reverted = at(10).isoformat(), at(70).isoformat()
    assert main(["observed-rto", str(log.path), "--onset", onset, "--reverted", reverted]) == 0
    assert "RTO 62.0 s" in capsys.readouterr().out
    late = at(90).isoformat()
    assert main(["observed-rto", str(log.path), "--onset", onset, "--reverted", late]) == 1
    assert "unobserved" in capsys.readouterr().out
    assert main(["observed-rto", str(log.path), "--onset", "2026-11-20T12:00:10",
                 "--reverted", reverted]) == 2  # fmt: skip
    assert "no time zone" in capsys.readouterr().err
    (tmp_path / "bad.csv").write_text("n,at\n")
    assert main(["verify-observations", str(tmp_path / "bad.csv")]) == 1
