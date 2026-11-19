"""RTO and RPO from observed samples and ledger heads (B-26b: FR-08.1, FR-08.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rai.core.scenarios import RecoveryPlan, ScenarioDefinition
from rai.recovery.measure import Head, Sample, rpo, rto

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def samples(*healthy: tuple[float, bool]) -> list[Sample]:
    return [Sample(at(s), ok) for s, ok in healthy]


def test_rto_runs_from_onset_to_the_start_of_a_stable_recovery() -> None:
    seen = samples((31, False), (32, True), (33, True), (34, True), (35, True))
    measured = rto(T0, at(30), seen, stable=3)
    assert (measured.restored_at, measured.seconds) == (at(32), 32.0)


def test_a_flapping_recovery_counts_only_once_it_holds() -> None:
    seen = samples((31, True), (32, True), (33, False), (34, True), (35, True), (36, True))
    assert rto(T0, at(30), seen, stable=3).seconds == 34.0


def test_probes_while_a_fault_is_applied_do_not_count() -> None:
    seen = samples((10, True), (20, True), (29, True), (30, True), (31, True))
    assert rto(T0, at(30), seen, stable=3).seconds is None  # only two after the revert
    assert rto(T0, at(30), seen, stable=2).seconds == 30.0


def test_no_restoration_has_no_rto() -> None:
    assert rto(T0, at(5), samples((6, False), (7, True)), stable=2).seconds is None
    assert rto(T0, at(5), [], stable=1).restored_at is None


def head(seq: int, rows: int, committed: float | None = 0) -> Head:
    return Head(seq, None if committed is None else at(committed), rows)


def test_nothing_lost_is_an_rpo_of_zero() -> None:
    result = rpo(T0, head(100, 100, -2), head(140, 140, 50))
    assert (result.seconds, result.lost_rows) == (0.0, 0)
    assert result.recovery_point == head(100, 100, -2)
    assert rpo(T0, head(100, 100), head(100, 100)).seconds == 0.0  # no writes at all


def test_a_ledger_that_went_back_dates_its_recovery_point() -> None:
    result = rpo(T0, head(100, 100, -1), head(90, 90, -45.5))
    assert (result.seconds, result.lost_rows) == (45.5, 10)
    assert result.recovery_point == head(90, 90, -45.5)


def test_lost_rows_behind_new_writes_make_rpo_unknown() -> None:
    result = rpo(T0, head(100, 100), head(120, 95, 30))
    assert (result.seconds, result.lost_rows, result.recovery_point) == (None, 5, None)
    assert rpo(T0, head(100, 100), head(90, 90, None)).seconds is None  # undatable


@pytest.mark.parametrize(
    ("body", "parsed"),
    [
        ({"seq": 7, "committed_at": "2026-11-17T12:00:00Z", "rows": 7}, head(7, 7)),
        ({"seq": None, "committed_at": None, "rows": 0}, Head(0, None, 0)),  # an empty ledger
        # SandboxBank's own field name (GET /v1/ledger/head returns ``count``).
        ({"seq": 7, "committed_at": "2026-11-17T12:00:00Z", "count": 7}, head(7, 7)),
        ({"seq": None, "committed_at": None, "count": 0}, Head(0, None, 0)),
    ],
)
def test_the_ledger_head_is_parsed(body: dict[str, Any], parsed: Head) -> None:
    assert Head.parse(body) == parsed


def test_sandboxbanks_own_ledger_head_parses() -> None:
    """The contract between the two: what SandboxBank's API returns (``asdict`` of its
    LedgerHead, then JSON) is a head RAI can read. A fake that said ``rows`` hid this."""
    import json
    from dataclasses import asdict

    from sandboxbank.ledger import LedgerHead

    sent = LedgerHead(seq=12, committed_at=at(3), count=12)
    body = json.loads(json.dumps(asdict(sent), default=lambda d: d.isoformat()))
    assert Head.parse(body) == Head(12, at(3), 12)
    assert Head.parse(json.loads(json.dumps(asdict(LedgerHead(None, None, 0))))) == Head(0, None, 0)


@pytest.mark.parametrize("body", [[], {"seq": 1}, {"seq": "x", "rows": 1}, {"rows": 1,
                                  "committed_at": "noon"}])  # fmt: skip
def test_an_unusable_ledger_head_is_refused(body: Any) -> None:
    with pytest.raises(ValueError, match="ledger head"):
        Head.parse(body)


def test_recovery_plans_are_checked() -> None:
    plan = RecoveryPlan(probes=("payments-api",), ledger="payments-ledger", rpo_seconds=0)
    assert (plan.window_seconds, plan.stable_checks) == (300, 3)
    with pytest.raises(ValueError, match=r"needs recovery.ledger"):
        RecoveryPlan(probes=("payments-api",), rpo_seconds=0)
    with pytest.raises(ValueError, match="not a target name"):
        RecoveryPlan(probes=("../etc",))
    definition = ScenarioDefinition.model_validate({
        "blast_radius": ["ledger-db-proxy"], "recovery": {"probes": ["payments-api"]},
        "faults": [{"type": "connection_drop", "target": "ledger-db-proxy",
                    "duration_seconds": 5}],
    })  # fmt: skip
    assert definition.recovery is not None
