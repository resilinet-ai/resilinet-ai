"""Tenant members: list, invite, change role, remove (B-16: FR-01.1).

Every change is written to the tenant's audit chain by the memberships trigger,
attributed to the signed-in admin (threats R-01, S-04).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError

from rai.api.context import ROLES, Role, TenantSession, require
from rai.api.problems import Problem

router = APIRouter(prefix="/v1/members", tags=["members"])

anyone = require(*ROLES)
admin = require("admin")
CHECK_VIOLATION = "23514"


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    status: str
    role: Role
    granted_at: datetime


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: str = Field(min_length=1, max_length=200)
    role: Role


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


class Accepted(BaseModel):
    status: str = "accepted"
    detail: str = (
        "If the address can be invited, it is now a member of this tenant with the given "
        "role. Existing members keep their role; change it with PATCH."
    )


_SELECT = (
    "SELECT m.user_id, u.email::text AS email, u.display_name, u.status::text AS status, "
    "m.role::text AS role, m.granted_at "
    "FROM rai.memberships m JOIN rai.users u USING (user_id)"
)


def _member(row: Row[tuple[object, ...]]) -> MemberOut:
    return MemberOut.model_validate(row._asdict())


@router.get("")
def list_members(session: Annotated[TenantSession, Depends(anyone)]) -> list[MemberOut]:
    rows = session.conn.execute(text(f"{_SELECT} ORDER BY u.email")).all()
    return [_member(r) for r in rows]


@router.post("", status_code=202)
def invite(body: InviteRequest, session: Annotated[TenantSession, Depends(admin)]) -> Accepted:
    """The same 202 whether the email is new, known in another tenant or already a
    member here, so the answer reveals nothing about other tenants (threat I-03)."""
    session.conn.execute(
        text("SELECT rai.invite_member(:e, :n, CAST(:r AS rai.member_role))"),
        {"e": body.email, "n": body.display_name, "r": body.role},
    )
    return Accepted()


def _not_self(session: TenantSession, user_id: UUID) -> None:
    if user_id == session.user_id:
        raise Problem(
            403, "members.self_change", "You cannot change or remove your own membership",
            detail="ask another admin of this tenant",
        )  # fmt: skip


def _last_admin(error: DBAPIError) -> Problem:
    if getattr(error.orig, "sqlstate", None) == CHECK_VIOLATION:
        return Problem(409, "members.last_admin", "A tenant must keep at least one admin")
    raise error


@router.patch("/{user_id}")
def change_role(
    user_id: UUID, body: RoleChange, session: Annotated[TenantSession, Depends(admin)]
) -> MemberOut:
    _not_self(session, user_id)
    try:
        changed = session.conn.execute(
            text(
                "UPDATE rai.memberships SET role = CAST(:r AS rai.member_role), "
                "granted_by = :by, granted_at = clock_timestamp() WHERE user_id = :u "
                "RETURNING user_id"
            ),
            {"r": body.role, "by": session.user_id, "u": user_id},
        ).first()
    except DBAPIError as error:
        raise _last_admin(error) from None
    if changed is None:
        raise Problem(404, "members.not_found", "Member not found")
    row = session.conn.execute(text(f"{_SELECT} WHERE m.user_id = :u"), {"u": user_id}).one()
    return _member(row)


@router.delete("/{user_id}", status_code=204)
def remove(user_id: UUID, session: Annotated[TenantSession, Depends(admin)]) -> Response:
    _not_self(session, user_id)
    try:
        removed = session.conn.execute(
            text("DELETE FROM rai.memberships WHERE user_id = :u RETURNING user_id"),
            {"u": user_id},
        ).first()
    except DBAPIError as error:
        raise _last_admin(error) from None
    if removed is None:
        raise Problem(404, "members.not_found", "Member not found")
    return Response(status_code=204)
