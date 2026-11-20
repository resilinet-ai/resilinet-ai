"""Release decisions (B-42: FR-12.1, FR-12.2).

* ``POST /v1/projects/{id}/releases/{label}/decisions`` records a named reviewer's
  approve or reject for one release, with a justification. Approve is refused while
  the release gate is blocked, and from anyone who asked for, or owns the finding of,
  a waiver the gate relies on.
* ``GET /v1/projects/{id}/releases`` lists the project's releases with their latest
  decision; ``GET /v1/projects/{id}/releases/{label}`` gives one release's history.

Each decision records the gate as it stood at that moment: its verdict, the findings
blocking it, and the findings waived with the waivers that lifted them. The database
computes that record and enforces both refusals (migration 0014), and audits every row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.problems import Problem
from rai.api.routes_projects import CHECK_VIOLATION, find_project
from rai.api.routes_waivers import GATE_DEFINITION

router = APIRouter(prefix="/v1", tags=["releases"])

anyone = require(*ROLES, key_scope="runs:read")
reviewer = require("reviewer", "admin")

LABEL = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
Label = Annotated[str, Path(pattern=LABEL, description="for example v1.4")]


class ReleaseDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    justification: str = Field(min_length=1, max_length=2000)


class GateAtDecision(BaseModel):
    verdict: Literal["pass", "blocked"]
    blocking_findings: list[UUID]
    waived_findings: list[UUID]
    waivers: list[UUID]  # the waiver in force for each waived finding, in the same order
    definition: str = GATE_DEFINITION


class ReleaseDecisionOut(BaseModel):
    decision_id: UUID
    project_id: UUID
    release_label: str
    decision: Literal["approve", "reject"]
    justification: str
    reviewer_id: UUID
    created_at: datetime
    gate: GateAtDecision


class ReleaseOut(BaseModel):
    project_id: UUID
    release_label: str
    status: Literal["approved", "rejected"]  # the latest decision
    latest: ReleaseDecisionOut
    decisions: list[ReleaseDecisionOut]  # oldest first


class ReleaseSummary(BaseModel):
    release_label: str
    status: Literal["approved", "rejected"]
    decided_at: datetime
    reviewer_id: UUID


_DECISION = (
    "SELECT decision_id, project_id, release_label, decision::text, justification, reviewer_id, "
    "created_at, gate_verdict, blocking_findings, waived_findings, waivers "
    "FROM rai.release_decisions"
)


def _decision(row: object) -> ReleaseDecisionOut:
    r = row._asdict()  # type: ignore[attr-defined]
    gate = GateAtDecision(
        verdict=r.pop("gate_verdict"), blocking_findings=r.pop("blocking_findings"),
        waived_findings=r.pop("waived_findings"), waivers=r.pop("waivers"),
    )  # fmt: skip
    return ReleaseDecisionOut.model_validate(r | {"gate": gate})


def find_release(conn: Connection, project_id: UUID, label: str) -> ReleaseOut:
    rows = conn.execute(
        text(f"{_DECISION} WHERE project_id = :p AND release_label = :l "
             "ORDER BY created_at, decision_id"),
        {"p": project_id, "l": label},
    ).all()  # fmt: skip
    if not rows:
        raise Problem(404, "releases.not_found", "No decision has been recorded for this release")
    decisions = [_decision(r) for r in rows]
    latest = decisions[-1]
    status: Literal["approved", "rejected"] = (
        "approved" if latest.decision == "approve" else "rejected"
    )
    return ReleaseOut(project_id=project_id, release_label=label, status=status,
                      latest=latest, decisions=decisions)  # fmt: skip


def _own_project(session: TenantSession, project_id: UUID) -> None:
    if not session.own_project(project_id):
        raise Problem(404, "projects.not_found", "Project not found")


@router.post("/projects/{project_id}/releases/{release_label}/decisions", status_code=201)
def decide_release(
    project_id: UUID,
    release_label: Label,
    body: ReleaseDecisionIn,
    response: Response,
    session: Annotated[TenantSession, Depends(reviewer)],
) -> ReleaseOut:
    """Approve or reject a release. Append-only: the latest decision counts."""
    find_project(session.conn, project_id, for_change=True)
    try:
        with session.conn.begin_nested():
            session.conn.execute(
                text("INSERT INTO rai.release_decisions "
                     "(project_id, release_label, decision, justification, reviewer_id) "
                     "VALUES (:p, :l, CAST(:d AS rai.approval_decision), :j, :u)"),
                {"p": project_id, "l": release_label, "d": body.decision,
                 "j": body.justification, "u": session.user_id},
            )  # fmt: skip
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) == CHECK_VIOLATION:
            raise _refusal(str(error.orig)) from None
        raise
    response.headers["Location"] = f"/v1/projects/{project_id}/releases/{release_label}"
    return find_release(session.conn, project_id, release_label)


def _refusal(message: str) -> Problem:
    if "gate is blocked" in message:
        return Problem(
            409, "releases.gate_blocked", "The release gate is blocked",
            detail="an open critical finding has no waiver in force; fix it or have it "
                   "waived, then decide again (GET /v1/projects/{id}/gate)",
        )  # fmt: skip
    if "your own waiver" in message:
        return Problem(
            403, "releases.self_approval",
            "You cannot approve a release that relies on your own waiver or finding",
            detail="separation of duties: another named reviewer must approve",
        )  # fmt: skip
    return Problem(409, "releases.refused", "The database refused the decision")


@router.get("/projects/{project_id}/releases")
def list_releases(
    project_id: UUID, session: Annotated[TenantSession, Depends(anyone)]
) -> list[ReleaseSummary]:
    """Every release of the project with its latest decision, most recent first."""
    _own_project(session, project_id)
    find_project(session.conn, project_id)
    rows = session.conn.execute(
        text("SELECT DISTINCT ON (release_label) release_label, "
             "CASE decision WHEN 'approve' THEN 'approved' ELSE 'rejected' END AS status, "
             "created_at AS decided_at, reviewer_id "
             "FROM rai.release_decisions WHERE project_id = :p "
             "ORDER BY release_label, created_at DESC, decision_id DESC"),
        {"p": project_id},
    ).all()  # fmt: skip
    found = [ReleaseSummary.model_validate(r._asdict()) for r in rows]
    return sorted(found, key=lambda s: (s.decided_at, s.release_label), reverse=True)


@router.get("/projects/{project_id}/releases/{release_label}")
def get_release(
    project_id: UUID, release_label: Label, session: Annotated[TenantSession, Depends(anyone)]
) -> ReleaseOut:
    _own_project(session, project_id)
    find_project(session.conn, project_id)
    return find_release(session.conn, project_id, release_label)
