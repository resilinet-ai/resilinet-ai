"""The executive dashboard's numbers (FR-11.1, NFR-USE-1).

``GET /v1/assurance/summary?project_id=...`` returns a project's assurance over a
window (default: the last 30 days): validation rule checks, the rules failing most,
runs by outcome, chaos scenario steps and each scenario's latest result, RTO and RPO
attainment, and findings. Every number comes with its denominator and a definition,
and the window applies to all of them except the findings open now (NFR-USE-1).
There is deliberately no composite score: it would have no denominator.

Read-only aggregates over the tenant's own rows (RLS). An API key with ``runs:read``
sees its own project only; others are 404 to it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text

from rai.api.context import ROLES, TenantSession, require
from rai.api.problems import Problem
from rai.api.routes_projects import find_project
from rai.api.routes_recovery import Attainment, attainment, window

router = APIRouter(prefix="/v1/assurance/summary", tags=["summary"])
anyone = require(*ROLES, key_scope="runs:read")

TOP_RULES = 10
LATEST_SCENARIOS = 10
WINDOW_DEFINITION = (
    "Runs and steps that finished, and recovery measurements whose fault onset fell, in "
    "[since, until), UTC. Open findings are counted now, whatever the window."
)
VALIDATION_DEFINITION = (
    "Rule checks (rule_result observations) that passed, over rule checks that passed, "
    "failed or errored, in runs that finished in the window. Skipped checks are left out. "
    "Counts rule checks, not rows."
)
ISSUES_DEFINITION = (
    "Rule checks that failed or errored, by rule, in runs that finished in the window; "
    "the most frequent first. Counts rule checks, not rows."
)
RUNS_DEFINITION = "Runs that finished in the window, by final state."
REFUSED_DEFINITION = (
    "Runs a preflight stopped before any test or fault ran (NFR-SAF-1), over runs that "
    "finished in the window."
)
CHAOS_DEFINITION = (
    "Chaos scenario steps that passed (faults injected and reverted, recovery objectives "
    "met), over scenario steps that finished or errored in the window."
)
FINDINGS_DEFINITION = (
    "Open findings now, by severity; and findings opened in the window, whatever their status now."
)

Severity = Literal["critical", "high", "medium", "low"]
SEVERITIES: tuple[Severity, ...] = ("critical", "high", "medium", "low")


class Ratio(BaseModel):
    numerator: int
    denominator: int
    definition: str


class RuleIssues(BaseModel):
    rule: str
    failing_checks: int


class Issues(BaseModel):
    total: int  # failing checks over every rule, not only those listed
    rules: list[RuleIssues]  # at most TOP_RULES
    definition: str


class Runs(BaseModel):
    finished: int
    by_state: dict[str, int]  # passed, failed, refused, aborted, error
    definition: str


class ScenarioResult(BaseModel):
    scenario_id: UUID
    name: str
    version: int
    run_id: UUID
    step_key: str
    state: Literal["done", "error"]
    verdict: Literal["pass", "fail"] | None
    finished_at: datetime
    rto_met: bool | None
    rpo_met: bool | None


class Findings(BaseModel):
    open: dict[Severity, int]
    opened_in_window: int
    definition: str


class SummaryOut(BaseModel):
    project_id: UUID
    since: datetime
    until: datetime
    window_definition: str
    validation: Ratio
    issues: Issues
    runs: Runs
    refused: Ratio
    chaos: Ratio
    scenarios: list[ScenarioResult]  # each scenario's latest step, newest first
    rto: Attainment
    rpo: Attainment
    recovery_measurements: int
    findings: Findings


_CHECKS = (
    "SELECT count(*) FILTER (WHERE o.outcome = 'pass') AS passed, "
    "count(*) FILTER (WHERE o.outcome <> 'skipped') AS judged "
    "FROM rai.observations o JOIN rai.runs r ON r.run_id = o.run_id "
    "WHERE r.project_id = :p AND r.finished_at >= :since AND r.finished_at < :until "
    "AND o.kind = 'rule_result'"
)
_FAILING = (
    "SELECT o.rule, count(*) AS n "
    "FROM rai.observations o JOIN rai.runs r ON r.run_id = o.run_id "
    "WHERE r.project_id = :p AND r.finished_at >= :since AND r.finished_at < :until "
    "AND o.kind = 'rule_result' AND o.outcome IN ('fail', 'error') "
    "GROUP BY o.rule ORDER BY n DESC, o.rule"
)
_STATES = (
    "SELECT r.state::text AS state, count(*) AS n FROM rai.runs r "
    "WHERE r.project_id = :p AND r.finished_at >= :since AND r.finished_at < :until "
    "GROUP BY 1"
)
# A scenario step is a run step whose pinned plan item is a scenario. The window
# counts are taken over every step before keeping each scenario's latest one.
_SCENARIOS = (
    "SELECT * FROM ("
    "  SELECT CAST(i->'scenario'->>'scenario_id' AS uuid) AS scenario_id, "
    "  i->'scenario'->>'name' AS name, CAST(i->'scenario'->>'version' AS integer) AS version, "
    "  st.run_id, st.step_key, st.state::text AS state, st.verdict::text AS verdict, "
    "  st.finished_at, m.rto_met, m.rpo_met, "
    "  count(*) OVER () AS steps, "
    "  count(*) FILTER (WHERE st.verdict = 'pass') OVER () AS passed, "
    "  row_number() OVER (PARTITION BY i->'scenario'->>'scenario_id' "
    "    ORDER BY st.finished_at DESC, st.run_id DESC) AS newest "
    "  FROM rai.run_steps st JOIN rai.runs r ON r.run_id = st.run_id "
    "  CROSS JOIN LATERAL jsonb_array_elements(r.pins->'items') i "
    "  LEFT JOIN rai.recovery_measurements m "
    "    ON m.run_id = st.run_id AND m.step_key = st.step_key "
    "  WHERE r.project_id = :p AND st.finished_at >= :since AND st.finished_at < :until "
    "  AND i->>'step_key' = st.step_key AND jsonb_typeof(i->'scenario') = 'object'"
    ") x WHERE newest = 1 ORDER BY finished_at DESC, run_id DESC"
)


@router.get("")
def summary(
    session: Annotated[TenantSession, Depends(anyone)],
    project_id: Annotated[UUID, Query()],
    since: Annotated[datetime | None, Query(description="start of the window (ISO 8601)")] = None,
    until: Annotated[datetime | None, Query(description="end of the window (ISO 8601)")] = None,
) -> SummaryOut:
    """Every dashboard number for [since, until), each with its denominator and definition."""
    if not session.own_project(project_id):
        raise Problem(404, "projects.not_found", "Project not found")
    find_project(session.conn, project_id)
    start, end = window(since, until, "summary")
    conn = session.conn
    params = {"p": project_id, "since": start, "until": end}

    checks = conn.execute(text(_CHECKS), params).one()
    failing = conn.execute(text(_FAILING), params).all()
    by_state = {row.state: int(row.n) for row in conn.execute(text(_STATES), params)}
    finished = sum(by_state.values())
    latest = conn.execute(text(_SCENARIOS), params).all()
    steps, passed = (latest[0].steps, latest[0].passed) if latest else (0, 0)
    recovery = attainment(conn, project_id, start, end)
    open_rows = conn.execute(
        text(
            "SELECT severity::text AS severity, count(*) AS n FROM rai.findings "
            "WHERE project_id = :p AND status = 'open' GROUP BY 1"
        ),
        params,
    ).all()
    opened = conn.execute(
        text(
            "SELECT count(*) FROM rai.findings WHERE project_id = :p "
            "AND created_at >= :since AND created_at < :until"
        ),
        params,
    ).scalar_one()
    open_by = {row.severity: int(row.n) for row in open_rows}

    return SummaryOut(
        project_id=project_id,
        since=start,
        until=end,
        window_definition=WINDOW_DEFINITION,
        validation=Ratio(
            numerator=checks.passed, denominator=checks.judged, definition=VALIDATION_DEFINITION
        ),
        issues=Issues(
            total=sum(int(row.n) for row in failing),
            rules=[RuleIssues(rule=row.rule, failing_checks=row.n) for row in failing[:TOP_RULES]],
            definition=ISSUES_DEFINITION,
        ),
        runs=Runs(finished=finished, by_state=by_state, definition=RUNS_DEFINITION),
        refused=Ratio(
            numerator=by_state.get("refused", 0),
            denominator=finished,
            definition=REFUSED_DEFINITION,
        ),
        chaos=Ratio(numerator=passed, denominator=steps, definition=CHAOS_DEFINITION),
        scenarios=[
            ScenarioResult.model_validate(row._asdict()) for row in latest[:LATEST_SCENARIOS]
        ],
        rto=recovery.rto,
        rpo=recovery.rpo,
        recovery_measurements=recovery.measurements,
        findings=Findings(
            open={s: open_by.get(s, 0) for s in SEVERITIES},
            opened_in_window=opened,
            definition=FINDINGS_DEFINITION,
        ),
    )
