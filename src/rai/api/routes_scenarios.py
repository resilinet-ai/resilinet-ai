"""Chaos scenarios and their review (B-26: FR-07.2, NFR-SAF-1; threats T-05, R-02, E-03).

* Engineers publish scenario versions: a blast radius and the faults to run in
  order (:class:`rai.core.scenarios.ScenarioDefinition`). Versions are immutable;
  publishing again creates the next one.
* Reviewers (or admins) approve or reject a version. Decisions are append-only and
  the latest one counts: only an approved version can be started in a run, and the
  worker checks again before it injects anything. **Nobody reviews their own
  scenario**: the API refuses it (403) and a database trigger refuses it anyway.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.listing import Page, PageOut, after_uuid, page, paged
from rai.api.problems import Problem
from rai.api.routes_projects import find_project, is_unique_violation
from rai.core.scenarios import (
    APPROVAL_TERM,
    MAX_APPROVAL_TERM,
    Decision,
    RecoveryPlan,
    ScenarioDefinition,
    ScenarioFault,
    latest_decision,
)

router = APIRouter(prefix="/v1", tags=["scenarios"])

anyone = require(*ROLES)
engineer = require("engineer")
reviewer = require("reviewer", "admin")

NAME = r"^[a-z][a-z0-9_.-]{0,99}$"
CHECK_VIOLATION = "23514"
Status = Literal["pending", "approved", "rejected", "expired"]


class ScenarioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)
    description: str | None = Field(default=None, max_length=2000)
    blast_radius: list[str] = Field(
        min_length=1, max_length=20, description="every target the scenario may touch"
    )
    faults: list[ScenarioFault] = Field(min_length=1, max_length=20, description="run in order")
    recovery: RecoveryPlan | None = Field(
        default=None, description="probes, ledger and RTO/RPO objectives (B-26b)"
    )


class ScenarioOut(BaseModel):
    scenario_id: UUID
    project_id: UUID
    name: str
    version: int
    description: str | None
    blast_radius: list[str]
    faults: list[ScenarioFault]
    recovery: RecoveryPlan | None
    status: Status  # from the latest review decision
    created_by: UUID
    created_at: datetime


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)
    expires_at: datetime | None = Field(
        default=None,
        description="approve only: when the approval lapses; default 30 days, at most 90 (B-43)",
    )


class ApprovalOut(BaseModel):
    approval_id: UUID
    scenario_id: UUID
    reviewer_id: UUID
    decision: Literal["approve", "reject"]
    comment: str | None
    created_at: datetime
    expires_at: datetime | None  # approvals only; None on approvals given before B-43
    expired: bool  # an approval past its expiry, or one without an expiry


class ScenarioDetail(ScenarioOut):
    approvals: list[ApprovalOut]  # oldest first


_SCENARIO = (
    "SELECT s.scenario_id, s.project_id, s.name, s.version, s.description, s.blast_radius, "
    "s.faults, s.recovery, s.created_by, s.created_at, COALESCE(("
    "  SELECT CASE WHEN a.decision = 'reject' THEN 'rejected' "
    "    WHEN a.expires_at IS NULL OR a.expires_at <= clock_timestamp() THEN 'expired' "
    "    ELSE 'approved' END "
    "  FROM rai.scenario_approvals a WHERE a.scenario_id = s.scenario_id "
    "  ORDER BY a.created_at DESC, a.approval_id DESC LIMIT 1), 'pending') AS status "
    "FROM rai.chaos_scenarios s"
)
_APPROVAL = (
    "SELECT approval_id, scenario_id, reviewer_id, decision::text, comment, created_at, "
    "expires_at, (decision = 'approve' AND coalesce(expires_at <= clock_timestamp(), true)) "
    "AS expired FROM rai.scenario_approvals"
)


def find_scenario(conn: Connection, scenario_id: UUID) -> ScenarioOut:
    row = conn.execute(text(f"{_SCENARIO} WHERE s.scenario_id = :s"), {"s": scenario_id}).first()
    if row is None:
        raise Problem(404, "scenarios.not_found", "Scenario not found")
    return ScenarioOut.model_validate(row._asdict())


@router.get("/projects/{project_id}/scenarios")
def list_scenarios(
    project_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    name: Annotated[str | None, Query(max_length=100)] = None,
) -> PageOut[ScenarioOut]:
    find_project(session.conn, project_id)
    rows = session.conn.execute(
        text(
            f"{_SCENARIO} WHERE s.project_id = :p "
            "AND (CAST(:name AS text) IS NULL OR s.name = :name) "
            "AND (CAST(:after AS uuid) IS NULL OR s.scenario_id > :after) "
            "ORDER BY s.scenario_id LIMIT :n"
        ),
        {"p": project_id, "name": name, "after": after_uuid(wanted), "n": wanted.limit + 1},
    ).all()
    items = [ScenarioOut.model_validate(r._asdict()) for r in rows]
    return paged(items, wanted, lambda s: str(s.scenario_id))


@router.post("/projects/{project_id}/scenarios", status_code=201)
def add_scenario(
    project_id: UUID,
    body: ScenarioIn,
    response: Response,
    session: Annotated[TenantSession, Depends(engineer)],
) -> ScenarioOut:
    """Publish the next version of the scenario called ``name`` (version 1 if new). It
    starts ``pending``: a reviewer other than you must approve it before it can run."""
    find_project(session.conn, project_id, for_change=True)
    try:
        definition = ScenarioDefinition(
            blast_radius=tuple(body.blast_radius), faults=tuple(body.faults),
            recovery=body.recovery,
        )  # fmt: skip
    except ValidationError as error:
        raise Problem(
            422, "scenarios.invalid", "The scenario is not consistent",
            detail="; ".join(e["msg"].removeprefix("Value error, ") for e in error.errors()),
        ) from None  # fmt: skip
    try:
        scenario_id = session.conn.execute(
            text(
                "INSERT INTO rai.chaos_scenarios (project_id, name, version, description, "
                "blast_radius, faults, recovery, created_by) "
                "SELECT :p, :n, COALESCE(MAX(version), 0) + 1, :d, :r, CAST(:f AS jsonb), "
                "CAST(:rec AS jsonb), :u "
                "FROM rai.chaos_scenarios WHERE project_id = :p AND name = :n "
                "RETURNING scenario_id"
            ),
            {
                "p": project_id, "n": body.name, "d": body.description,
                "r": list(definition.blast_radius), "u": session.user_id,
                "f": "[" + ",".join(f.model_dump_json() for f in definition.faults) + "]",
                "rec": definition.recovery.model_dump_json() if definition.recovery else None,
            },
        ).scalar_one()  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(
                409, "definitions.concurrent_publish", "Another version was published; retry"
            ) from None
        raise
    response.headers["Location"] = f"/v1/scenarios/{scenario_id}"
    return find_scenario(session.conn, scenario_id)


@router.get("/scenarios/{scenario_id}")
def get_scenario(
    scenario_id: UUID, session: Annotated[TenantSession, Depends(anyone)]
) -> ScenarioDetail:
    scenario = find_scenario(session.conn, scenario_id)
    approvals = session.conn.execute(
        text(f"{_APPROVAL} WHERE scenario_id = :s ORDER BY created_at, approval_id"),
        {"s": scenario_id},
    ).all()
    return ScenarioDetail.model_validate(
        scenario.model_dump() | {"approvals": [a._asdict() for a in approvals]}
    )


@router.post("/scenarios/{scenario_id}/approvals", status_code=201)
def review_scenario(
    scenario_id: UUID,
    body: ApprovalIn,
    response: Response,
    session: Annotated[TenantSession, Depends(reviewer)],
) -> ApprovalOut:
    """Approve or reject a scenario version. Append-only: the latest decision counts."""
    scenario = find_scenario(session.conn, scenario_id)
    if scenario.created_by == session.user_id:
        raise _self_review()
    find_project(session.conn, scenario.project_id, for_change=True)
    expires_at = _expiry(session.conn, body)
    try:
        with session.conn.begin_nested():
            approval_id = session.conn.execute(
                text(
                    "INSERT INTO rai.scenario_approvals (scenario_id, reviewer_id, decision, "
                    "comment, expires_at) VALUES (:s, :u, CAST(:d AS rai.approval_decision), "
                    ":c, :e) RETURNING approval_id"
                ),
                {"s": scenario_id, "u": session.user_id, "d": body.decision,
                 "c": body.comment, "e": expires_at},
            ).scalar_one()  # fmt: skip
    except IntegrityError as error:  # the database's own separation-of-duties check
        if getattr(error.orig, "sqlstate", None) == CHECK_VIOLATION:
            raise _self_review() from None
        raise
    response.headers["Location"] = f"/v1/scenarios/{scenario_id}"
    row = session.conn.execute(text(f"{_APPROVAL} WHERE approval_id = :a"), {"a": approval_id})
    return ApprovalOut.model_validate(row.one()._asdict())


def _expiry(conn: Connection, body: ApprovalIn) -> datetime | None:
    """An approval's expiry (B-43): the one asked for, or 30 days; a rejection has none."""
    if body.decision == "reject":
        if body.expires_at is not None:
            raise Problem(422, "scenarios.expiry_on_reject", "Only an approval has an expiry")
        return None
    now: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    if body.expires_at is None:
        return now + APPROVAL_TERM
    if body.expires_at.tzinfo is None:
        raise Problem(422, "scenarios.expiry_without_zone", "expires_at needs a time zone")
    if not now < body.expires_at <= now + MAX_APPROVAL_TERM:
        raise Problem(422, "scenarios.expiry_out_of_range",
                      "expires_at must be in the future and at most 90 days ahead")  # fmt: skip
    return body.expires_at


def _self_review() -> Problem:
    return Problem(
        403, "scenarios.self_review", "You cannot review your own scenario",
        detail="separation of duties: another reviewer must decide",
    )  # fmt: skip


def runnable_decision(conn: Connection, scenario_id: UUID) -> Decision | None:
    """The latest decision on the version, which a run may pin only if it is in force."""
    return latest_decision(conn, scenario_id)
