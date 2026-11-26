"""Ground truth for RPO and post-recovery damage (H3's RPO half, H4; the author's
decisions of 2026-11-26)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rai_bench.alerts import Alert, write_alerts
from rai_bench.chaos_run import damage_flagged, rpo_agreement, score_chaos
from rai_bench.truth import Reconciler, acknowledged, compare
from sandboxbank.observer import ObservationLog, Sample
from sandboxbank.seeder import AckLog, Attempt

T0 = datetime(2027, 2, 20, 10, 0, tzinfo=UTC)


class Bank:
    """SandboxBank as the reconciliation reads it; tests change it between reads."""

    def __init__(self) -> None:
        self.ledger: set[str] = set()
        self.posted: set[int] = set()
        self.facts: Counter[int] = Counter()
        self.feats: Counter[int] = Counter()
        self.on_read: list[Any] = []  # called once per settle round

    def ledger_has(self, txn_ids: list[str]) -> set[str]:
        return {t for t in txn_ids if t in self.ledger}

    def ledger_posted(self) -> set[int]:
        return set(self.posted)

    def warehouse(self) -> Counter[int]:
        if self.on_read:
            self.on_read.pop(0)()
        return Counter(self.facts)

    def features(self) -> Counter[int]:
        return Counter(self.feats)


def acks(path: Path, *writes: tuple[str, str, float]) -> None:
    log = AckLog(path)
    for txn, outcome, seconds in writes:
        log.append(Attempt(txn, outcome, None, T0 + timedelta(seconds=seconds)))


class Ticks:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def healthy_bank() -> Bank:
    bank = Bank()
    bank.ledger = {"a", "b", "c"}
    bank.posted = {1, 2, 3}
    bank.facts = Counter({1: 1, 2: 1, 3: 1})
    bank.feats = Counter({1: 1, 2: 1, 3: 1})
    return bank


def test_compare_finds_missing_duplicated_and_unexpected() -> None:
    found = compare({1, 2, 3, 4}, Counter({1: 1, 2: 2, 9: 1}), due=3)
    assert found == {"missing": {3}, "duplicated": {2}, "unexpected": {9}}  # 4 is not due yet


def test_lost_acknowledged_writes_are_damage_and_date_the_rpo(tmp_path: Path) -> None:
    bank = healthy_bank()
    acks(tmp_path / "acks.csv", ("a", "created", 1), ("b", "replayed", 2), ("x", "created", 40),
         ("y", "unacknowledged", 45), ("z", "created", 50))  # fmt: skip
    assert [t for t, _ in acknowledged(tmp_path / "acks.csv")] == ["a", "b", "x", "z"]
    check = Reconciler(bank, tmp_path / "acks.csv", tmp_path / "state.json")
    truth = check({"onset_at": (T0 + timedelta(seconds=60)).isoformat()})
    # x and z were acknowledged and are not in the ledger; y was never a promise.
    assert truth["new"]["lost_acked"] == 2
    assert truth["sample"]["lost_acked"] == ["x", "z"]
    assert truth["rpo_truth_seconds"] == 20.0  # onset 60 s minus the earliest loss at 40 s
    assert truth["lost"]
    assert truth["damaged"]
    # The same losses are not attributed again, even after a restart.
    again = Reconciler(bank, tmp_path / "acks.csv", tmp_path / "state.json")
    later = again({"onset_at": (T0 + timedelta(seconds=600)).isoformat()})
    assert later["new"]["lost_acked"] == 0
    assert (later["lost"], later["damaged"], later["rpo_truth_seconds"]) == (False, False, 0.0)
    # A loss with no measured onset cannot be dated.
    acks(tmp_path / "acks.csv", ("w", "created", 700))
    assert again({"onset_at": None})["rpo_truth_seconds"] is None


def test_a_job_catching_up_is_not_damage_but_one_that_never_does_is(tmp_path: Path) -> None:
    acks(tmp_path / "acks.csv", ("a", "created", 1))
    bank = healthy_bank()
    bank.posted |= {4, 5}  # the ETL is two rows behind when the check starts
    # Reads: the P3 cutoff, round 1 (still behind), round 2 (caught up).
    bank.on_read = [lambda: None, lambda: None, lambda: bank.facts.update({4: 1, 5: 1})]
    ticks = Ticks()
    check = Reconciler(bank, tmp_path / "acks.csv", tmp_path / "state.json", within=900,
                       interval=10, sleep=ticks.sleep, monotonic=ticks.monotonic)  # fmt: skip
    caught_up = check({"onset_at": T0.isoformat()})
    assert caught_up["new"]["p2.missing"] == 0
    assert caught_up["new"]["p3.missing"] == 0  # 4 and 5 were not in the warehouse yet
    assert caught_up["waited_seconds"] == 10.0
    assert not caught_up["damaged"]
    # Now the ETL doubles a row and never loads seq 6; features gain a phantom.
    bank.posted.add(6)
    bank.facts.update({2: 1})
    bank.feats.update({4: 1, 5: 1, 99: 1})
    stuck = check({"onset_at": T0.isoformat()})
    assert stuck["new"] | {"lost_acked": 0} == {
        "lost_acked": 0, "p2.missing": 1, "p2.duplicated": 1, "p2.unexpected": 0,
        "p3.missing": 0, "p3.duplicated": 0, "p3.unexpected": 1,
    }  # fmt: skip
    assert stuck["waited_seconds"] >= 900
    assert stuck["damaged"]
    assert not stuck["lost"]


def test_an_edited_seeding_log_is_refused(tmp_path: Path) -> None:
    acks(tmp_path / "acks.csv", ("a", "created", 1), ("b", "created", 2))
    text = (tmp_path / "acks.csv").read_text().replace(",b,", ",c,")
    (tmp_path / "acks.csv").write_text(text)
    with pytest.raises(ValueError, match="chain"):
        acknowledged(tmp_path / "acks.csv")


def row(n: int, *, rai_lost: int, truth: dict[str, Any] | None, failed: bool = False,
        onset: float = 0) -> dict[str, Any]:  # fmt: skip
    start = T0 + timedelta(seconds=onset)
    return {"format": "rai-bench-chaos-run/1", "injection_id": f"I{n:04d}", "scenario": "S01",
            "measured": True, "onset_at": start.isoformat(),
            "reverted_at": (start + timedelta(seconds=60)).isoformat(), "rto_seconds": 80.0,
            "rto_met": True, "rpo_met": rai_lost == 0, "rpo_seconds": 30.0 if rai_lost else 0.0,
            "lost_rows": rai_lost, "window_seconds": 600, "stable_checks": 3,
            "probes": ["payments-health"], "ledger": "payments-ledger",
            "blast_radius": ["ledger-db-proxy"], "truth": truth,
            "consistency": {"item-2": {"missing": {"outcome": "fail" if failed else "pass",
                                                   "count": 3 if failed else 0}}}}  # fmt: skip


def truth(*, lost: bool = False, damaged: bool = False, rpo: float | None = 0.0) -> dict[str, Any]:
    new = {"lost_acked": int(lost), "p2.missing": int(damaged and not lost)}
    return {"lost": lost, "damaged": damaged or lost, "new": new, "rpo_truth_seconds": rpo}


def test_h3_rpo_scores_the_loss_verdict() -> None:
    rows = [row(1, rai_lost=0, truth=truth()),                       # both: nothing lost
            row(2, rai_lost=4, truth=truth(lost=True, rpo=31.5)),    # both lost
            row(3, rai_lost=0, truth=truth(lost=True, rpo=12.0)),    # RAI missed a loss
            row(4, rai_lost=0, truth=None)]  # fmt: skip
    result = rpo_agreement(rows)
    assert result["summary"] | {"ci95": None} == {"scored": 3, "agreed": 2, "not_scored": 1,
                                                  "share": 0.6667, "ci95": None}  # fmt: skip
    assert result["injections"]["I0002"]["error_seconds"] == -1.5  # descriptive only
    assert result["injections"]["I0003"]["agrees"] is False


def test_h4_counts_who_flagged_new_damage() -> None:
    rows = [row(1, rai_lost=0, truth=truth(damaged=True), failed=True),  # RAI's check failed
            row(2, rai_lost=2, truth=truth(lost=True)),                  # RAI's RPO found it
            row(3, rai_lost=0, truth=truth(damaged=True)),               # nobody
            row(4, rai_lost=0, truth=truth(), failed=True)]  # fmt: skip  # RAI, on clean data
    found = {"post_restoration": {"I0001": ["B2"], "I0003": ["B1"]}}
    h4 = damage_flagged(rows, found)
    s = h4["summary"]
    assert (s["injections"], s["damaged"]) == (4, 3)
    assert s["arms"]["R"] == {"flagged_damaged": 2, "flagged_clean": 1}
    assert s["arms"]["B2"] == {"flagged_damaged": 1, "flagged_clean": 0}
    assert s["arms"]["B1"] == {"flagged_damaged": 1, "flagged_clean": 0}
    assert s["r_not_b2"] == 1  # I0002


def test_only_a_baseline_alert_after_restoration_flags_damage(tmp_path: Path) -> None:
    # The API is down 0-80 s; the observer sees it restored at 80 s.
    log = ObservationLog(tmp_path / "obs.csv")
    for i in range(int(800 / 0.25)):
        t = i * 0.25
        log.append(Sample(T0 + timedelta(seconds=t), 0 if t < 80 else 200, True, i,
                          etl=200, features=200))  # fmt: skip
    rows = [row(1, rai_lost=0, truth=truth(damaged=True), failed=True),
            row(2, rai_lost=0, truth=truth(damaged=True), onset=400)]  # fmt: skip
    (tmp_path / "injections.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def alert(arm: str, seconds: float) -> Alert:
        return Alert(arm=arm, subject_kind="injection", subject_id="live",
                     time=T0 + timedelta(seconds=seconds), location=("payments-api",),
                     message="failing")  # fmt: skip

    # B2 pages during the outage only; B1's check fails after restoration (at 120 s).
    write_alerts(tmp_path / "alerts.jsonl", [alert("B2", 3), alert("B1", 120)])
    result = score_chaos(tmp_path / "injections.jsonl", tmp_path / "obs.csv",
                         tmp_path / "alerts.jsonl")  # fmt: skip
    flags = result["h4"]["injections"]["I0001"]["flagged"]
    assert flags == {"R": True, "B1": True, "B2": False}
    assert result["h4"]["summary"]["r_not_b2"] == 1
    assert result["h3_rpo"]["summary"]["agreed"] == 2
