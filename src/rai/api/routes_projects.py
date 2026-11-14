"""Assurance projects and pipeline assets (B-17: FR-01.1, FR-04.1, FR-11.2).

Write roles follow the Data Model endpoint catalogue: projects by admins and
engineers, assets by engineers. Every role in the tenant can read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.listing import Page, PageOut, after_uuid, etag, if_match, page, paged
from rai.api.problems import Problem

router = APIRouter(prefix="/v1", tags=["projects"])

anyone = require(*ROLES)
project_writer = require("admin", "engineer")
engineer = require("engineer")

UNIQUE_VIOLATION = "23505"
Classification = Literal["synthetic", "authorized_snapshot"]
AssetKind = Literal["source", "table", "transform", "feature_set", "model"]


class ProjectOut(BaseModel):
    project_id: UUID
    name: str
    description: str | None
    owner_id: UUID
    data_classification: Classification
    default_environment_id: UUID | None
    status: Literal["active", "archived"]
    revision: int
    created_at: datetime
    updated_at: datetime


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner_id: UUID | None = None  # default: the caller
    data_classification: Classification = "synthetic"


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner_id: UUID | None = None
    data_classification: Classification | None = None
    status: Literal["active", "archived"] | None = None


class AssetOut(BaseModel):
    asset_id: UUID
    project_id: UUID
    kind: AssetKind
    name: str
    locator: str | None
    schema_version: int | None
    created_by: UUID
    created_at: datetime


class AssetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: AssetKind
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    locator: str | None = Field(default=None, min_length=1, max_length=500)
    schema_version: int | None = Field(default=None, ge=1)


_PROJECT = (
    "SELECT project_id, name, description, owner_id, data_classification::text, "
    "default_environment_id, status::text, row_version AS revision, created_at, updated_at "
    "FROM rai.assurance_projects"
)
_ASSET = (
    "SELECT asset_id, project_id, kind::text, name, locator, schema_version, created_by, "
    "created_at FROM rai.pipeline_assets"
)


def is_unique_violation(error: IntegrityError) -> bool:
    return getattr(error.orig, "sqlstate", None) == UNIQUE_VIOLATION


def find_project(conn: Connection, project_id: UUID, *, for_change: bool = False) -> ProjectOut:
    """The project, or 404 (also for another tenant's project: RLS hides it).
    With ``for_change`` an archived project is refused (409)."""
    row = conn.execute(text(f"{_PROJECT} WHERE project_id = :p"), {"p": project_id}).first()
    if row is None:
        raise Problem(404, "projects.not_found", "Project not found")
    project = ProjectOut.model_validate(row._asdict())
    if for_change and project.status == "archived":
        raise Problem(409, "projects.archived", "The project is archived; reactivate it first")
    return project


def _check_owner(conn: Connection, owner_id: UUID) -> None:
    member = conn.execute(
        text("SELECT 1 FROM rai.memberships WHERE user_id = :u"), {"u": owner_id}
    ).first()
    if member is None:
        raise Problem(422, "projects.owner_not_member", "The owner must be a member of this tenant")


def _send_project(response: Response, project: ProjectOut) -> ProjectOut:
    response.headers["ETag"] = etag(project.revision)
    return project


@router.get("/projects")
def list_projects(
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    status: Annotated[Literal["active", "archived"] | None, Query()] = None,
) -> PageOut[ProjectOut]:
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_PROJECT} WHERE (CAST(:after AS uuid) IS NULL OR project_id > :after) "
            "AND (CAST(:status AS text) IS NULL OR status::text = :status) "
            "ORDER BY project_id LIMIT :n"
        ),
        {"after": after, "status": status, "n": wanted.limit + 1},
    ).all()
    projects = [ProjectOut.model_validate(r._asdict()) for r in rows]
    return paged(projects, wanted, lambda p: str(p.project_id))


@router.post("/projects", status_code=201)
def create_project(
    body: ProjectIn, response: Response, session: Annotated[TenantSession, Depends(project_writer)]
) -> ProjectOut:
    owner = body.owner_id or session.user_id
    _check_owner(session.conn, owner)
    try:
        project_id = session.conn.execute(
            text(
                "INSERT INTO rai.assurance_projects (name, description, owner_id, "
                "data_classification) VALUES (:n, :d, :o, CAST(:c AS rai.data_classification)) "
                "RETURNING project_id"
            ),
            {"n": body.name, "d": body.description, "o": owner, "c": body.data_classification},
        ).scalar_one()
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(409, "projects.name_taken", "A project with this name exists") from None
        raise
    response.headers["Location"] = f"/v1/projects/{project_id}"
    return _send_project(response, find_project(session.conn, project_id))


@router.get("/projects/{project_id}")
def get_project(
    project_id: UUID, response: Response, session: Annotated[TenantSession, Depends(anyone)]
) -> ProjectOut:
    return _send_project(response, find_project(session.conn, project_id))


@router.patch("/projects/{project_id}")
def update_project(
    project_id: UUID,
    body: ProjectPatch,
    response: Response,
    revision: Annotated[int, Depends(if_match)],
    session: Annotated[TenantSession, Depends(project_writer)],
) -> ProjectOut:
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    cleared = sorted(k for k, v in changes.items() if v is None and k != "description")
    if cleared:
        raise Problem(
            422, "request.invalid", "Only description may be set to null",
            detail=f"cannot clear: {', '.join(cleared)}",
        )  # fmt: skip
    if "owner_id" in changes:
        _check_owner(session.conn, changes["owner_id"])
    casts = {"data_classification": "rai.data_classification", "status": "rai.project_status"}
    sets = [
        f"{column} = CAST(:{column} AS {casts[column]})"
        if column in casts
        else f"{column} = :{column}"
        for column in changes
    ]
    if not sets:
        return _send_project(response, find_project(session.conn, project_id))
    try:
        updated = session.conn.execute(
            text(
                f"UPDATE rai.assurance_projects SET {', '.join(sets)} "  # noqa: S608 - fixed column names
                "WHERE project_id = :project_id AND row_version = :revision RETURNING project_id"
            ),
            changes | {"project_id": project_id, "revision": revision},
        ).first()
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(409, "projects.name_taken", "A project with this name exists") from None
        raise
    if updated is None:
        find_project(session.conn, project_id)  # 404 if it does not exist
        raise Problem(412, "request.stale", "The resource has changed; read it again")
    return _send_project(response, find_project(session.conn, project_id))


# ---- pipeline assets ---------------------------------------------------------------------


@router.get("/projects/{project_id}/assets")
def list_assets(
    project_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
) -> PageOut[AssetOut]:
    find_project(session.conn, project_id)
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_ASSET} WHERE project_id = :p "
            "AND (CAST(:after AS uuid) IS NULL OR asset_id > :after) ORDER BY asset_id LIMIT :n"
        ),
        {"p": project_id, "after": after, "n": wanted.limit + 1},
    ).all()
    assets = [AssetOut.model_validate(r._asdict()) for r in rows]
    return paged(assets, wanted, lambda a: str(a.asset_id))


@router.post("/projects/{project_id}/assets", status_code=201)
def register_asset(
    project_id: UUID,
    body: AssetIn,
    response: Response,
    session: Annotated[TenantSession, Depends(engineer)],
) -> AssetOut:
    find_project(session.conn, project_id, for_change=True)
    try:
        asset_id = session.conn.execute(
            text(
                "INSERT INTO rai.pipeline_assets (project_id, kind, name, locator, schema_version, "
                "created_by) VALUES (:p, CAST(:k AS rai.asset_kind), :n, :l, :s, :u) "
                "RETURNING asset_id"
            ),
            {
                "p": project_id, "k": body.kind, "n": body.name, "l": body.locator,
                "s": body.schema_version, "u": session.user_id,
            },
        ).scalar_one()  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(
                409, "assets.name_taken", "This project has an asset with this name"
            ) from None
        raise
    response.headers["Location"] = f"/v1/assets/{asset_id}"
    return find_asset(session.conn, asset_id)


def find_asset(conn: Connection, asset_id: UUID) -> AssetOut:
    row = conn.execute(text(f"{_ASSET} WHERE asset_id = :a"), {"a": asset_id}).first()
    if row is None:
        raise Problem(404, "assets.not_found", "Asset not found")
    return AssetOut.model_validate(row._asdict())


@router.get("/assets/{asset_id}")
def get_asset(asset_id: UUID, session: Annotated[TenantSession, Depends(anyone)]) -> AssetOut:
    return find_asset(session.conn, asset_id)
