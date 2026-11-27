"""Recovery measurements: what the Recovery screen and the dashboard show (B-29).

``GET /v1/assurance/recovery?project_id=...`` returns the project's recovery
measurements in a window (default: the last 30 days) with their attainment: RTO and
RPO objectives met, over the measurements that had an objective (NFR-USE-1: every
number with its denominator, window and definition). Rows come from the insert-only
``recovery_measurements`` table, written by the worker with the step they measure.
An API key with ``runs:read`` sees its own project only; others are 404 to it.

Each measurement also carries what its run found after recovery (the Recovery screen,
W08): the run's post-recovery **consistency** findings (FR-08.3), summed over its
``consistency_check`` steps, and the **seeding-log RPO** (``recovery.rpo_truth``, B-30)
where the run had one, with whether it agrees with the ledger on whether anything was
lost. ``consistency`` attainment counts the measurements whose run checked consistency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from rai.api.context import ROLES, TenantSession, require
from rai.api.problems import Problem
from rai.api.routes_projects import find_project

router = APIRouter(prefix="/v1/assurance/recovery", tags=["recovery"])
anyone = require(*ROLES, key_scope="runs:read")

DEFAULT_WINDOW = timedelta(days=30)
MAX_WINDOW = timedelta(days=366)
RTO_DEFINITION = (
    "First fault onset to the first of the scenario's stable_checks consecutive healthy "
    "probe rounds after the last revert; not restored within the window is a miss."
)
RPO_DEFINITION = (
    "0 s if no committed ledger row was lost, otherwise onset minus the surviving head's "
    "commit time; lost rows or an unreadable ledger is a miss."
)
CONSISTENCY_DEFINITION = (
    "Runs whose post-recovery consistency checks found no missing, duplicated, unexpected "
    "or mismatched keys, over the measured runs that ran such a check (FR-08.3)."
)
FINDINGS = ("missing", "duplicated", "unexpected", "mismatched")


class ConsistencyOut(BaseModel):
    """The run's post-recovery consistency findings, summed over its check steps."""

    missing: int
    duplicated: int
    unexpected: int
    mismatched: int
    passed: bool  # no finding failed (counted-only findings, e.g. allowed extras, do not)


class RpoTruthOut(BaseModel):
    """RPO from SandboxBank's seeding log (B-30), beside the ledger's own account."""

    seconds: float | None
    lost_acknowledged: int
    agrees: bool | None  # same verdict as the ledger on whether anything was lost


class MeasurementOut(BaseModel):
    measurement_id: UUID
    run_id: UUID
    step_key: str
    scenario_id: UUID
    scenario_name: str
    scenario_version: int
    environment_id: UUID | None
    method: str
    onset_at: datetime
    reverted_at: datetime
    restored_at: datetime | None
    rto_seconds: Decimal | None
    rto_objective_seconds: int | None
    rto_met: bool | None
    rpo_attempted: bool
    rpo_seconds: Decimal | None
    rpo_objective_seconds: int | None
    rpo_met: bool | None
    lost_rows: int | None
    recorded_at: datetime
    consistency: ConsistencyOut | None = None  # None: the run checked no consistency
    rpo_truth: RpoTruthOut | None = None  # None: the run had no seeding-log check


class Attainment(BaseModel):
    met: int
    with_objective: int  # the denominator: measurements that had this objective
    definition: str


class RecoveryOut(BaseModel):
    project_id: UUID
    scenario_id: UUID | None
    since: datetime
    until: datetime
    measurements: int  # n in the window
    not_restored: int
    rto: Attainment
    rpo: Attainment
    consistency: Attainment
    items: list[MeasurementOut]  # newest first, at most ``limit``
    truncated: bool


# The run's consistency observations, one row per measurement (no row: no check ran).
_CONSISTENCY = (
    "LEFT JOIN LATERAL (SELECT count(*) AS n, bool_and(o.outcome <> 'fail') AS ok, "
    "coalesce(sum(o.failing_count) FILTER (WHERE o.rule = 'consistency.missing'), 0) AS missing, "
    "coalesce(sum(o.failing_count) FILTER (WHERE o.rule = 'consistency.duplicated'), 0) "
    "AS duplicated, "
    "coalesce(sum(o.failing_count) FILTER (WHERE o.rule = 'consistency.unexpected'), 0) "
    "AS unexpected, "
    "coalesce(sum(o.failing_count) FILTER (WHERE o.rule = 'consistency.mismatched'), 0) "
    "AS mismatched "
    "FROM rai.observations o WHERE o.run_id = m.run_id AND o.rule LIKE 'consistency.%') c "
    "ON true "
)
# The run's seeding-log RPO (B-30), if it had one.
_TRUTH = (
    "(SELECT o.observed_value FROM rai.observations o WHERE o.run_id = m.run_id "
    "AND o.rule = 'recovery.rpo_truth' ORDER BY o.observed_at LIMIT 1)"
)
_WINDOW = (
    "FROM rai.recovery_measurements m JOIN rai.runs r ON r.run_id = m.run_id "
    "JOIN rai.chaos_scenarios s ON s.scenario_id = m.scenario_id "
    f"{_CONSISTENCY}"
    "WHERE r.project_id = :p AND (CAST(:s AS uuid) IS NULL OR m.scenario_id = :s) "
    "AND m.onset_at >= :since AND m.onset_at < :until"
)


@router.get("")
def recovery(
    session: Annotated[TenantSession, Depends(anyone)],
    project_id: Annotated[UUID, Query()],
    scenario_id: Annotated[UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="onset at or after (ISO 8601)")] = None,
    until: Annotated[datetime | None, Query(description="onset before (ISO 8601)")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RecoveryOut:
    """Measurements whose onset falls in [since, until), with RTO and RPO attainment."""
    if not session.own_project(project_id):
        raise Problem(404, "projects.not_found", "Project not found")
    find_project(session.conn, project_id)
    start, end = window(since, until, "recovery")
    params = {"p": project_id, "s": scenario_id, "since": start, "until": end}
    counts = attainment(session.conn, project_id, start, end, scenario_id)
    rows = session.conn.execute(
        text(
            "SELECT m.measurement_id, m.run_id, m.step_key, m.scenario_id, "
            "s.name AS scenario_name, s.version AS scenario_version, r.environment_id, "
            "m.method, m.onset_at, m.reverted_at, m.restored_at, m.rto_seconds, "
            "m.rto_objective_seconds, m.rto_met, m.rpo_attempted, m.rpo_seconds, "
            "m.rpo_objective_seconds, m.rpo_met, m.lost_rows, m.recorded_at, c.n AS c_n, "
            "c.ok AS c_ok, c.missing, c.duplicated, c.unexpected, c.mismatched, "
            f"{_TRUTH} AS truth {_WINDOW} ORDER BY m.onset_at DESC, m.measurement_id DESC LIMIT :n"
        ),
        {**params, "n": limit},
    ).all()
    return RecoveryOut(
        project_id=project_id, scenario_id=scenario_id, since=start, until=end,
        measurements=counts.measurements, not_restored=counts.not_restored,
        rto=counts.rto, rpo=counts.rpo, consistency=counts.consistency,
        items=[_measurement(r._asdict()) for r in rows],
        truncated=counts.measurements > len(rows),
    )  # fmt: skip


def _measurement(row: dict[str, Any]) -> MeasurementOut:
    c_n, c_ok, truth = row.pop("c_n"), row.pop("c_ok"), row.pop("truth")
    found = {f: int(row.pop(f)) for f in FINDINGS}
    if c_n:
        row["consistency"] = ConsistencyOut(**found, passed=bool(c_ok))
    if isinstance(truth, dict):
        lost = int(truth.get("lost_acknowledged") or 0)
        seconds = truth.get("seconds")
        agrees = None if row["lost_rows"] is None else (lost > 0) == (row["lost_rows"] > 0)
        row["rpo_truth"] = RpoTruthOut(
            seconds=None if seconds is None else float(seconds), lost_acknowledged=lost,
            agrees=agrees,
        )  # fmt: skip
    return MeasurementOut.model_validate(row)


@dataclass(frozen=True, slots=True)
class Counts:
    measurements: int
    not_restored: int
    rto: Attainment
    rpo: Attainment
    consistency: Attainment


def attainment(
    conn: Connection, project_id: UUID, start: datetime, end: datetime,
    scenario_id: UUID | None = None,
) -> Counts:  # fmt: skip
    """RTO and RPO attainment of the measurements whose onset is in [start, end)."""
    row = conn.execute(
        text(
            "SELECT count(*) AS n, count(*) FILTER (WHERE m.restored_at IS NULL) AS unrestored, "
            "count(*) FILTER (WHERE m.rto_met) AS rto_met, count(m.rto_met) AS rto_n, "
            "count(*) FILTER (WHERE m.rpo_met) AS rpo_met, count(m.rpo_met) AS rpo_n, "
            "count(*) FILTER (WHERE c.n > 0 AND c.ok) AS c_met, "
            f"count(*) FILTER (WHERE c.n > 0) AS c_n {_WINDOW}"
        ),
        {"p": project_id, "s": scenario_id, "since": start, "until": end},
    ).one()
    return Counts(
        measurements=row.n, not_restored=row.unrestored,
        rto=Attainment(met=row.rto_met, with_objective=row.rto_n, definition=RTO_DEFINITION),
        rpo=Attainment(met=row.rpo_met, with_objective=row.rpo_n, definition=RPO_DEFINITION),
        consistency=Attainment(met=row.c_met, with_objective=row.c_n,
                               definition=CONSISTENCY_DEFINITION),
    )  # fmt: skip


def window(since: datetime | None, until: datetime | None, code: str) -> tuple[datetime, datetime]:
    """[since, until): default the last 30 days up to now, at most 366 days."""
    end = _aware(until) or datetime.now(UTC)
    start = _aware(since) or end - DEFAULT_WINDOW
    if start >= end:
        raise Problem(422, f"{code}.window_empty", "since must be before until")
    if end - start > MAX_WINDOW:
        raise Problem(422, f"{code}.window_too_long", "The window may be at most 366 days")
    return start, end


def _aware(moment: datetime | None) -> datetime | None:
    """Naive timestamps are read as UTC."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)
