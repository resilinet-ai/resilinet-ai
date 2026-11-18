"""Waivers with expiry, and the release gate (B-33: FR-12.1, FR-12.2).

* ``POST /v1/findings/{id}/waivers`` asks to waive an open critical finding, with a
  reason and an expiry at most 90 days ahead. One waiver at a time: not while another
  is pending or in force.
* ``POST /v1/waivers/{id}/decisions`` approves or rejects it: a named reviewer (or
  admin) other than the requester and the finding's owner. Append-only; the latest
  decision counts, so a later ``reject`` withdraws an approval.
* ``GET /v1/projects/{id}/gate`` is the release gate: ``blocked`` while any open
  critical finding has no waiver in force. An expired waiver stops counting by itself.

The database enforces the same rules (migration 0012) and audits every row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.problems import Problem
from rai.api.routes_findings import find_finding
from rai.api.routes_projects import CHECK_VIOLATION, find_project

router = APIRouter(prefix="/v1", tags=["waivers"])

anyone = require(*ROLES)
writer = require("engineer", "reviewer", "admin")
reviewer = require("reviewer", "admin")
gate_reader = require(*ROLES, key_scope="runs:read")

MAX_TERM = timedelta(days=90)
GATE_LIMIT = 100
GATE_DEFINITION = (
    "blocked while any open critical finding of the project has no waiver in force; "
    "a waiver is in force while its latest decision approves it, it has not expired, "
    "and its finding is open"
)


class WaiverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: datetime = Field(description="at most 90 days ahead (ISO 8601 with a zone)")


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


class DecisionOut(BaseModel):
    decision_id: UUID
    reviewer_id: UUID
    decision: Literal["approve", "reject"]
    comment: str | None
    created_at: datetime


class WaiverOut(BaseModel):
    waiver_id: UUID
    finding_id: UUID
    reason: str
    expires_at: datetime
    requested_by: UUID
    created_at: datetime
    status: Literal["pending", "approved", "rejected"]  # the latest decision
    expired: bool
    in_force: bool  # approved, not expired, and the finding is open
    decisions: list[DecisionOut]  # oldest first


class GateFinding(BaseModel):
    finding_id: UUID
    title: str
    owner_id: UUID
    source_run_id: UUID
    waiver_id: UUID | None
    waiver_expires_at: datetime | None


class GateOut(BaseModel):
    project_id: UUID
    verdict: Literal["pass", "blocked"]
    evaluated_at: datetime
    blocking_count: int
    waived_count: int
    blocking: list[GateFinding]  # oldest first, at most 100
    waived: list[GateFinding]  # soonest expiry first, at most 100
    definition: str


_WAIVER = (
    "SELECT w.waiver_id, w.finding_id, w.reason, w.expires_at, w.requested_by, w.created_at, "
    "coalesce(CASE d.decision WHEN 'approve' THEN 'approved' WHEN 'reject' THEN 'rejected' END, "
    "'pending') AS status, w.expires_at <= now() AS expired, "
    "(d.decision = 'approve' AND w.expires_at > now() AND f.status = 'open') IS TRUE AS in_force "
    "FROM rai.waivers w JOIN rai.findings f USING (finding_id) "
    "LEFT JOIN LATERAL (SELECT decision FROM rai.waiver_decisions x "
    "  WHERE x.waiver_id = w.waiver_id ORDER BY x.created_at DESC, x.decision_id DESC LIMIT 1"
    ") d ON true"
)
_DECISIONS = (
    "SELECT decision_id, reviewer_id, decision::text, comment, created_at "
    "FROM rai.waiver_decisions WHERE waiver_id = :w ORDER BY created_at, decision_id"
)


def find_waiver(conn: Connection, waiver_id: UUID) -> WaiverOut:
    row = conn.execute(text(f"{_WAIVER} WHERE w.waiver_id = :w"), {"w": waiver_id}).first()
    if row is None:
        raise Problem(404, "waivers.not_found", "Waiver not found")
    decisions = conn.execute(text(_DECISIONS), {"w": waiver_id}).all()
    return WaiverOut.model_validate(row._asdict() | {"decisions": [d._asdict() for d in decisions]})


@router.post("/findings/{finding_id}/waivers", status_code=201)
def request_waiver(
    finding_id: UUID,
    body: WaiverIn,
    response: Response,
    session: Annotated[TenantSession, Depends(writer)],
) -> WaiverOut:
    """Ask to waive an open critical finding until ``expires_at`` (at most 90 days)."""
    finding = find_finding(session.conn, finding_id, for_update=True)
    find_project(session.conn, finding.project_id, for_change=True)
    if finding.status != "open":
        raise Problem(409, "findings.resolved", f"The finding is {finding.status}")
    if finding.severity != "critical":
        raise Problem(422, "waivers.not_blocking",
                      f"A {finding.severity} finding does not block the gate; nothing to waive",
                      detail="only open critical findings block the release gate")  # fmt: skip
    if body.expires_at.tzinfo is None:
        raise Problem(422, "waivers.expiry_without_zone", "expires_at needs a time zone")
    now = session.conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    if not now < body.expires_at <= now + MAX_TERM:
        raise Problem(422, "waivers.expiry_out_of_range",
                      "expires_at must be in the future and at most 90 days ahead")  # fmt: skip
    current = session.conn.execute(
        text(f"{_WAIVER} WHERE w.finding_id = :f AND NOT w.expires_at <= now() "
             "AND (d.decision IS NULL OR d.decision = 'approve') LIMIT 1"),
        {"f": finding_id},
    ).first()  # fmt: skip
    if current is not None:
        raise Problem(409, "waivers.exists",
                      f"The finding already has a waiver that is {current.status}",
                      headers={"Location": f"/v1/waivers/{current.waiver_id}"})  # fmt: skip
    waiver_id = session.conn.execute(
        text("INSERT INTO rai.waivers (finding_id, reason, expires_at, requested_by) "
             "VALUES (:f, :r, :e, :u) RETURNING waiver_id"),
        {"f": finding_id, "r": body.reason, "e": body.expires_at, "u": session.user_id},
    ).scalar_one()  # fmt: skip
    response.headers["Location"] = f"/v1/waivers/{waiver_id}"
    return find_waiver(session.conn, waiver_id)


@router.get("/findings/{finding_id}/waivers")
def list_waivers(
    finding_id: UUID, session: Annotated[TenantSession, Depends(anyone)]
) -> list[WaiverOut]:
    """Every waiver asked for on the finding, oldest first."""
    find_finding(session.conn, finding_id)
    ids = session.conn.execute(
        text("SELECT waiver_id FROM rai.waivers WHERE finding_id = :f "
             "ORDER BY created_at, waiver_id"),
        {"f": finding_id},
    ).scalars().all()  # fmt: skip
    return [find_waiver(session.conn, w) for w in ids]


@router.get("/waivers/{waiver_id}")
def get_waiver(waiver_id: UUID, session: Annotated[TenantSession, Depends(anyone)]) -> WaiverOut:
    return find_waiver(session.conn, waiver_id)


@router.post("/waivers/{waiver_id}/decisions", status_code=201)
def decide_waiver(
    waiver_id: UUID, body: DecisionIn, session: Annotated[TenantSession, Depends(reviewer)]
) -> WaiverOut:
    """Approve or reject a waiver. Append-only: the latest decision counts."""
    waiver = find_waiver(session.conn, waiver_id)
    finding = find_finding(session.conn, waiver.finding_id, for_update=True)
    find_project(session.conn, finding.project_id, for_change=True)
    if session.user_id in (waiver.requested_by, finding.owner_id):
        raise _self_approval()
    if body.decision == "approve":
        if finding.status != "open":
            raise Problem(409, "findings.resolved", f"The finding is {finding.status}")
        if waiver.expired:
            raise Problem(409, "waivers.expired", "The waiver has expired; ask for a new one")
    try:
        with session.conn.begin_nested():
            session.conn.execute(
                text("INSERT INTO rai.waiver_decisions (waiver_id, reviewer_id, decision, comment) "
                     "VALUES (:w, :u, CAST(:d AS rai.approval_decision), :c)"),
                {"w": waiver_id, "u": session.user_id, "d": body.decision, "c": body.comment},
            )  # fmt: skip
    except IntegrityError as error:  # the database's own check, e.g. expiry passed meanwhile
        if getattr(error.orig, "sqlstate", None) == CHECK_VIOLATION:
            raise Problem(409, "waivers.refused", "The database refused the decision",
                          detail="the waiver expired, or its finding was resolved, "
                                 "while you decided") from None  # fmt: skip
        raise
    return find_waiver(session.conn, waiver_id)


def _self_approval() -> Problem:
    return Problem(
        403, "waivers.self_approval",
        "You cannot decide a waiver you asked for or on a finding you own",
        detail="separation of duties: another named reviewer must decide",
    )  # fmt: skip


@router.get("/projects/{project_id}/gate")
def release_gate(
    project_id: UUID, session: Annotated[TenantSession, Depends(gate_reader)]
) -> GateOut:
    """The release gate now: blocked while an open critical finding has no waiver in force."""
    if not session.own_project(project_id):
        raise Problem(404, "projects.not_found", "Project not found")
    find_project(session.conn, project_id)
    rows = session.conn.execute(
        text("SELECT f.finding_id, f.title, f.owner_id, f.source_run_id, "
             "w.waiver_id, w.expires_at AS waiver_expires_at "
             "FROM rai.findings f "
             "LEFT JOIN rai.waivers w ON w.waiver_id = rai.finding_waiver(f.finding_id) "
             "WHERE f.project_id = :p AND f.status = 'open' AND f.severity = 'critical' "
             "ORDER BY f.finding_id"),
        {"p": project_id},
    ).all()  # fmt: skip
    found = [GateFinding.model_validate(r._asdict()) for r in rows]
    blocking = [f for f in found if f.waiver_id is None]
    waived = sorted((f for f in found if f.waiver_id is not None),
                    key=lambda f: (f.waiver_expires_at, f.finding_id))  # fmt: skip
    evaluated_at = session.conn.execute(text("SELECT now()")).scalar_one()
    return GateOut(
        project_id=project_id,
        verdict="blocked" if blocking else "pass",
        evaluated_at=evaluated_at.astimezone(UTC),
        blocking_count=len(blocking),
        waived_count=len(waived),
        blocking=blocking[:GATE_LIMIT],
        waived=waived[:GATE_LIMIT],
        definition=GATE_DEFINITION,
    )
