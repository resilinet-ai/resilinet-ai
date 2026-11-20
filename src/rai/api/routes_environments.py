"""Environments and the target allow-list (B-22: NFR-SAF-1, FR-07.2).

Admins register environments and their targets; every role in the tenant can
read them. The API offers only SANDBOX and STAGING, and the database could not
store anything else (``env_type`` has no PRODUCTION value; ``production_flag`` is
held false). An environment's name and type never change; it is retired instead
of deleted, and a retired environment takes no new targets and runs nothing.

A container target names the image it runs (B-45: NFR-REP-1): ``image_digest``, Docker's
image ID. Runs pin it, and the chaos agent refuses a container running another image.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.listing import Page, PageOut, after_uuid, etag, if_match, page, paged
from rai.api.problems import Problem
from rai.api.routes_projects import is_unique_violation

router = APIRouter(prefix="/v1/environments", tags=["environments"])

anyone = require(*ROLES)
admin = require("admin")

EnvType = Literal["SANDBOX", "STAGING"]  # there is no PRODUCTION: NFR-SAF-1
Status = Literal["active", "retired"]
TargetKind = Literal["container", "proxy", "db", "http"]
NAME = r"^[A-Z][A-Z0-9-]{1,62}$"
MAX_FAULT_SECONDS = 3600


class EnvironmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME, description="e.g. SANDBOX-01; may not contain PROD")
    type: EnvType
    max_fault_duration_seconds: int = Field(default=900, ge=1, le=MAX_FAULT_SECONDS)

    @field_validator("name")
    @classmethod
    def _not_production(cls, value: str) -> str:
        if "PROD" in value:
            raise ValueError("environment names may not contain PROD (NFR-SAF-1)")
        return value


class EnvironmentPatch(BaseModel):
    """Only the fault ceiling and the status change; name and type never do."""

    model_config = ConfigDict(extra="forbid")
    max_fault_duration_seconds: int | None = Field(default=None, ge=1, le=MAX_FAULT_SECONDS)
    status: Status | None = None


class EnvironmentOut(BaseModel):
    environment_id: UUID
    name: str
    type: EnvType
    production_flag: bool
    max_fault_duration_seconds: int
    status: Status
    revision: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: TargetKind
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}$", description="e.g. ledger-db-proxy")
    required_label: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_./-]{0,99}=[A-Za-z0-9_.-]{1,100}$",
        description="key=value the target must carry; required for containers and proxies",
    )
    health_url: str | None = Field(default=None, pattern=r"^https?://\S{1,490}$")
    image_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="a container's image ID (docker inspect --format '{{.Image}}'); "
        "required for containers",
    )


class TargetOut(BaseModel):
    target_id: UUID
    environment_id: UUID
    kind: TargetKind
    name: str
    required_label: str | None
    health_url: str | None
    image_digest: str | None
    created_by: UUID
    created_at: datetime


_ENV = (
    "SELECT environment_id, name, type::text, production_flag, "
    "CAST(extract(epoch FROM max_fault_duration) AS integer) AS max_fault_duration_seconds, "
    "status::text, row_version AS revision, created_by, created_at, updated_at "
    "FROM rai.environments"
)
_TARGET = (
    "SELECT target_id, environment_id, kind::text, name, required_label, health_url, "
    "image_digest, created_by, created_at FROM rai.environment_targets"
)


def find_environment(
    conn: Connection, environment_id: UUID, *, active: bool = False
) -> EnvironmentOut:
    """The environment, or 404; with ``active`` a retired one is refused (409)."""
    row = conn.execute(text(f"{_ENV} WHERE environment_id = :e"), {"e": environment_id}).first()
    if row is None:
        raise Problem(404, "environments.not_found", "Environment not found")
    environment = EnvironmentOut.model_validate(row._asdict())
    if active and environment.status != "active":
        raise Problem(409, "environments.retired", "The environment is retired")
    return environment


def _send(response: Response, environment: EnvironmentOut) -> EnvironmentOut:
    response.headers["ETag"] = etag(environment.revision)
    return environment


@router.get("")
def list_environments(
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    status: Annotated[Status | None, Query()] = None,
) -> PageOut[EnvironmentOut]:
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_ENV} WHERE (CAST(:s AS text) IS NULL OR status::text = :s) "
            "AND (CAST(:after AS uuid) IS NULL OR environment_id > :after) "
            "ORDER BY environment_id LIMIT :n"
        ),
        {"s": status, "after": after, "n": wanted.limit + 1},
    ).all()
    found = [EnvironmentOut.model_validate(r._asdict()) for r in rows]
    return paged(found, wanted, lambda e: str(e.environment_id))


@router.post("", status_code=201)
def register_environment(
    body: EnvironmentIn, response: Response, session: Annotated[TenantSession, Depends(admin)]
) -> EnvironmentOut:
    try:
        with session.conn.begin_nested():
            environment_id = session.conn.execute(
                text(
                    "INSERT INTO rai.environments (name, type, max_fault_duration, created_by) "
                    "VALUES (:n, CAST(:t AS rai.env_type), make_interval(secs => :d), :u) "
                    "RETURNING environment_id"
                ),
                {"n": body.name, "t": body.type, "d": body.max_fault_duration_seconds,
                 "u": session.user_id},
            ).scalar_one()  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(
                409, "environments.name_taken", "An environment with this name exists"
            ) from None
        raise
    response.headers["Location"] = f"/v1/environments/{environment_id}"
    return _send(response, find_environment(session.conn, environment_id))


@router.get("/{environment_id}")
def get_environment(
    environment_id: UUID, response: Response, session: Annotated[TenantSession, Depends(anyone)]
) -> EnvironmentOut:
    return _send(response, find_environment(session.conn, environment_id))


@router.patch("/{environment_id}")
def update_environment(
    environment_id: UUID,
    body: EnvironmentPatch,
    response: Response,
    revision: Annotated[int, Depends(if_match)],
    session: Annotated[TenantSession, Depends(admin)],
) -> EnvironmentOut:
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise Problem(422, "request.invalid", "Fields cannot be set to null")
    if not changes:
        return _send(response, find_environment(session.conn, environment_id))
    updated = session.conn.execute(
        text(
            "UPDATE rai.environments SET "
            "max_fault_duration = COALESCE(make_interval(secs => CAST(:d AS integer)), "
            "max_fault_duration), status = COALESCE(CAST(:s AS rai.env_status), status) "
            "WHERE environment_id = :e AND row_version = :revision RETURNING environment_id"
        ),
        {"d": changes.get("max_fault_duration_seconds"), "s": changes.get("status"),
         "e": environment_id, "revision": revision},
    ).first()  # fmt: skip
    if updated is None:
        find_environment(session.conn, environment_id)  # 404 if it does not exist
        raise Problem(412, "request.stale", "The resource has changed; read it again")
    return _send(response, find_environment(session.conn, environment_id))


# ---- the target allow-list -------------------------------------------------------------------


@router.get("/{environment_id}/targets")
def list_targets(
    environment_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
) -> PageOut[TargetOut]:
    find_environment(session.conn, environment_id)
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_TARGET} WHERE environment_id = :e "
            "AND (CAST(:after AS uuid) IS NULL OR target_id > :after) "
            "ORDER BY target_id LIMIT :n"
        ),
        {"e": environment_id, "after": after, "n": wanted.limit + 1},
    ).all()
    targets = [TargetOut.model_validate(r._asdict()) for r in rows]
    return paged(targets, wanted, lambda t: str(t.target_id))


@router.post("/{environment_id}/targets", status_code=201)
def allow_target(
    environment_id: UUID,
    body: TargetIn,
    response: Response,
    session: Annotated[TenantSession, Depends(admin)],
) -> TargetOut:
    find_environment(session.conn, environment_id, active=True)
    if body.kind in ("container", "proxy") and body.required_label is None:
        raise Problem(
            422, "targets.label_required",
            "A container or proxy target must name the label it carries",
            detail="required_label, e.g. rai.sandbox=true: the chaos agent refuses targets "
            "without it",
        )  # fmt: skip
    if body.kind == "container" and body.image_digest is None:
        raise Problem(
            422, "targets.image_required", "A container target must name the image it runs",
            detail="image_digest: docker inspect --format '{{.Image}}' <container>. Runs pin "
            "it, and the chaos agent refuses a container running another image",
        )  # fmt: skip
    if body.kind != "container" and body.image_digest is not None:
        raise Problem(422, "targets.image_not_container", "Only a container target has an image")
    try:
        with session.conn.begin_nested():
            target_id = session.conn.execute(
                text(
                    "INSERT INTO rai.environment_targets (environment_id, kind, name, "
                    "required_label, health_url, image_digest, created_by) VALUES (:e, "
                    "CAST(:k AS rai.target_kind), :n, :l, :h, :i, :u) RETURNING target_id"
                ),
                {"e": environment_id, "k": body.kind, "n": body.name, "l": body.required_label,
                 "h": body.health_url, "i": body.image_digest, "u": session.user_id},
            ).scalar_one()  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(
                409, "targets.name_taken", "This environment already allows a target of this name"
            ) from None
        raise
    response.headers["Location"] = f"/v1/environments/{environment_id}/targets/{target_id}"
    row = session.conn.execute(text(f"{_TARGET} WHERE target_id = :t"), {"t": target_id}).one()
    return TargetOut.model_validate(row._asdict())


@router.delete("/{environment_id}/targets/{target_id}", status_code=204)
def remove_target(
    environment_id: UUID, target_id: UUID, session: Annotated[TenantSession, Depends(admin)]
) -> Response:
    """Take a target off the allow-list; nothing can be injected into it afterwards."""
    find_environment(session.conn, environment_id)
    removed = session.conn.execute(
        text("DELETE FROM rai.environment_targets WHERE target_id = :t AND environment_id = :e"),
        {"t": target_id, "e": environment_id},
    ).rowcount
    if not removed:
        raise Problem(404, "targets.not_found", "Target not found")
    return Response(status_code=204)
