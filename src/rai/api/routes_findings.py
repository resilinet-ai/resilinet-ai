"""Findings: severity, owner, fix and retest (FR-10.1).

* ``POST /v1/projects/{id}/findings`` opens a finding on a failing (or errored)
  observation of one of the project's runs: at most one finding per observation.
* ``PATCH /v1/findings/{id}`` sets the owner, the fix reference or the description
  while the finding is open. Title, severity and source never change.
* ``POST /v1/findings/{id}/retest`` closes it with a later, finished run of the
  project in which the same rule (for the same test) passed. Nothing else closes it.
* ``POST /v1/findings/{id}/dismiss`` ends it with a reason, by a reviewer or admin
  other than its creator.

The database enforces the same rules (migration 0011), audits every change, and
refuses deletes. Waivers and the release gate build on findings (B-33).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.listing import Page, PageOut, after_uuid, page, paged
from rai.api.problems import Problem
from rai.api.routes_projects import find_project, is_unique_violation

router = APIRouter(prefix="/v1", tags=["findings"])

anyone = require(*ROLES)
writer = require("engineer", "reviewer", "admin")
reviewer = require("reviewer", "admin")

Severity = Literal["critical", "high", "medium", "low"]
Status = Literal["open", "closed", "dismissed"]


class FindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    step_key: str = Field(min_length=1, max_length=200)
    rule: str = Field(min_length=1, max_length=300, description="the failing observation's rule")
    title: str = Field(min_length=1, max_length=200)
    severity: Severity
    description: str | None = Field(default=None, max_length=4000)
    owner_id: UUID | None = Field(default=None, description="a member; default: you")


class FindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: UUID | None = None
    fix_ref: str | None = Field(
        default=None, min_length=1, max_length=500, description="a pull request, commit or ticket"
    )
    description: str | None = Field(default=None, max_length=4000)


class RetestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID


class DismissIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class FindingOut(BaseModel):
    finding_id: UUID
    project_id: UUID
    title: str
    description: str | None
    severity: Severity
    status: Status
    source_run_id: UUID
    source_step_key: str
    source_rule: str
    owner_id: UUID
    fix_ref: str | None
    retest_run_id: UUID | None
    resolution: str | None
    resolved_by: UUID | None
    resolved_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


_FINDING = (
    "SELECT finding_id, project_id, title, description, severity::text, status::text, "
    "source_run_id, source_step_key, source_rule, owner_id, fix_ref, retest_run_id, "
    "resolution, resolved_by, resolved_at, created_by, created_at, updated_at "
    "FROM rai.findings"
)


def find_finding(conn: Connection, finding_id: UUID, *, for_update: bool = False) -> FindingOut:
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute(text(f"{_FINDING} WHERE finding_id = :f{lock}"), {"f": finding_id}).first()
    if row is None:
        raise Problem(404, "findings.not_found", "Finding not found")
    return FindingOut.model_validate(row._asdict())


def _member(conn: Connection, user_id: UUID) -> None:
    if conn.execute(
        text("SELECT 1 FROM rai.memberships WHERE user_id = :u"), {"u": user_id}
    ).first() is None:  # fmt: skip
        raise Problem(422, "findings.owner_not_member", "The owner must be a member of this tenant")


def _open(finding: FindingOut) -> None:
    if finding.status != "open":
        raise Problem(409, "findings.resolved", f"The finding is {finding.status}",
                      detail="a resolved finding never changes; open a new one")  # fmt: skip


@router.post("/projects/{project_id}/findings", status_code=201)
def open_finding(
    project_id: UUID,
    body: FindingIn,
    response: Response,
    session: Annotated[TenantSession, Depends(writer)],
) -> FindingOut:
    """Open a finding on a failing or errored observation of one of the project's runs."""
    find_project(session.conn, project_id, for_change=True)
    source = session.conn.execute(
        text("SELECT o.outcome::text, r.project_id FROM rai.observations o "
             "JOIN rai.runs r ON r.run_id = o.run_id "
             "WHERE o.run_id = :r AND o.step_key = :k AND o.rule = :rule"),
        {"r": body.run_id, "k": body.step_key, "rule": body.rule},
    ).first()  # fmt: skip
    if source is None or source.project_id != project_id:
        raise Problem(422, "findings.source_not_found",
                      "No such observation in a run of this project")  # fmt: skip
    if source.outcome not in ("fail", "error"):
        raise Problem(422, "findings.source_not_failing",
                      f"The observation {source.outcome}ed; a finding needs a failure",
                      detail="cite an observation whose outcome is fail or error")  # fmt: skip
    owner = body.owner_id or session.user_id
    _member(session.conn, owner)
    try:
        with session.conn.begin_nested():
            finding_id = session.conn.execute(
                text("INSERT INTO rai.findings (project_id, title, description, severity, "
                     "source_run_id, source_step_key, source_rule, owner_id, created_by) "
                     "VALUES (:p, :t, :d, CAST(:s AS rai.finding_severity), :r, :k, :rule, "
                     ":o, :u) RETURNING finding_id"),
                {"p": project_id, "t": body.title, "d": body.description, "s": body.severity,
                 "r": body.run_id, "k": body.step_key, "rule": body.rule, "o": owner,
                 "u": session.user_id},
            ).scalar_one()  # fmt: skip
    except IntegrityError as error:
        if not is_unique_violation(error):
            raise
        existing = session.conn.execute(
            text("SELECT finding_id FROM rai.findings WHERE source_run_id = :r "
                 "AND source_step_key = :k AND source_rule = :rule"),
            {"r": body.run_id, "k": body.step_key, "rule": body.rule},
        ).scalar_one()  # fmt: skip
        raise Problem(409, "findings.duplicate", "This observation already has a finding",
                      headers={"Location": f"/v1/findings/{existing}"}) from None  # fmt: skip
    response.headers["Location"] = f"/v1/findings/{finding_id}"
    return find_finding(session.conn, finding_id)


@router.get("/projects/{project_id}/findings")
def list_findings(
    project_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    status: Annotated[Status | None, Query()] = None,
    severity: Annotated[Severity | None, Query()] = None,
) -> PageOut[FindingOut]:
    """Oldest first, so a page boundary never moves; filter by status and severity."""
    find_project(session.conn, project_id)
    rows = session.conn.execute(
        text(f"{_FINDING} WHERE project_id = :p "
             "AND (CAST(:st AS text) IS NULL OR status::text = :st) "
             "AND (CAST(:sev AS text) IS NULL OR severity::text = :sev) "
             "AND (CAST(:after AS uuid) IS NULL OR finding_id > :after) "
             "ORDER BY finding_id LIMIT :n"),
        {"p": project_id, "st": status, "sev": severity, "after": after_uuid(wanted),
         "n": wanted.limit + 1},
    ).all()  # fmt: skip
    items = [FindingOut.model_validate(r._asdict()) for r in rows]
    return paged(items, wanted, lambda f: str(f.finding_id))


@router.get("/findings/{finding_id}")
def get_finding(finding_id: UUID, session: Annotated[TenantSession, Depends(anyone)]) -> FindingOut:
    return find_finding(session.conn, finding_id)


@router.patch("/findings/{finding_id}")
def update_finding(
    finding_id: UUID, body: FindingPatch, session: Annotated[TenantSession, Depends(writer)]
) -> FindingOut:
    """Owner, fix reference or description of an open finding."""
    finding = find_finding(session.conn, finding_id, for_update=True)
    _open(finding)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return finding
    if changes.get("owner_id") is None and "owner_id" in changes:
        raise Problem(422, "findings.owner_required", "A finding always has an owner")
    if "owner_id" in changes:
        _member(session.conn, changes["owner_id"])
    columns = ", ".join(f"{name} = :{name}" for name in changes)
    session.conn.execute(
        text(f"UPDATE rai.findings SET {columns} WHERE finding_id = :f"),  # noqa: S608 - fixed names
        {**changes, "f": finding_id},
    )
    return find_finding(session.conn, finding_id)


@router.post("/findings/{finding_id}/retest")
def retest_finding(
    finding_id: UUID, body: RetestIn, session: Annotated[TenantSession, Depends(writer)]
) -> FindingOut:
    """Close the finding with a retest: a later, finished run of the project in which the
    same rule passed for the same test. Anything else is 409 and the finding stays open."""
    finding = find_finding(session.conn, finding_id, for_update=True)
    _open(finding)
    run = session.conn.execute(
        text("SELECT project_id, state::text, queued_at FROM rai.runs WHERE run_id = :r"),
        {"r": body.run_id},
    ).first()
    if run is None or run.project_id != finding.project_id:
        raise Problem(422, "findings.retest_not_found", "No such run in this project")
    if run.state not in ("passed", "failed"):
        raise Problem(409, "findings.retest_unfinished",
                      f"The retest run is {run.state}; wait until it finishes")  # fmt: skip
    if run.queued_at <= finding.created_at:
        raise Problem(409, "findings.retest_too_early",
                      "The retest run started before the finding was opened")  # fmt: skip
    passes = session.conn.execute(
        text("SELECT rai.finding_retest_passes(:r, :k, :rule, :t)"),
        {"r": finding.source_run_id, "k": finding.source_step_key, "rule": finding.source_rule,
         "t": body.run_id},
    ).scalar_one()  # fmt: skip
    if not passes:
        raise Problem(
            409, "findings.retest_not_passing",
            f"The retest run does not pass {finding.source_rule!r}",
            detail="it must observe the same rule for the same test, pass, and not fail it",
        )  # fmt: skip
    session.conn.execute(
        text("UPDATE rai.findings SET status = 'closed', retest_run_id = :t, "
             "resolved_by = :u, resolved_at = clock_timestamp() WHERE finding_id = :f"),
        {"t": body.run_id, "u": session.user_id, "f": finding_id},
    )  # fmt: skip
    return find_finding(session.conn, finding_id)


@router.post("/findings/{finding_id}/dismiss")
def dismiss_finding(
    finding_id: UUID, body: DismissIn, session: Annotated[TenantSession, Depends(reviewer)]
) -> FindingOut:
    """End a finding that is not a defect, with the reason. Not by its creator."""
    finding = find_finding(session.conn, finding_id, for_update=True)
    _open(finding)
    if finding.created_by == session.user_id:
        raise Problem(403, "findings.self_dismissal", "You cannot dismiss a finding you opened",
                      detail="separation of duties: another reviewer must decide")  # fmt: skip
    session.conn.execute(
        text("UPDATE rai.findings SET status = 'dismissed', resolution = :reason, "
             "resolved_by = :u, resolved_at = clock_timestamp() WHERE finding_id = :f"),
        {"reason": body.reason, "u": session.user_id, "f": finding_id},
    )  # fmt: skip
    return find_finding(session.conn, finding_id)
