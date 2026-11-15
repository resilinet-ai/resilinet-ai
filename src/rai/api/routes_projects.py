"""Assurance projects, pipeline assets and their declared inputs (B-17, B-19: FR-01.1,
FR-04.1, FR-11.2).

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
CHECK_VIOLATION = "23514"
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
    default_environment_id: UUID | None = None  # B-22: where its runs execute by default


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner_id: UUID | None = None
    data_classification: Classification | None = None
    status: Literal["active", "archived"] | None = None
    default_environment_id: UUID | None = None


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


def check_environment(conn: Connection, environment_id: UUID) -> None:
    """An environment of this tenant that is active (B-22), else 422."""
    status = conn.execute(
        text("SELECT status::text FROM rai.environments WHERE environment_id = :e"),
        {"e": environment_id},
    ).scalar_one_or_none()
    if status != "active":
        raise Problem(
            422, "environments.not_usable", "The environment does not exist or is retired"
        )


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
    if body.default_environment_id is not None:
        check_environment(session.conn, body.default_environment_id)
    try:
        project_id = session.conn.execute(
            text(
                "INSERT INTO rai.assurance_projects (name, description, owner_id, "
                "data_classification, default_environment_id) VALUES (:n, :d, :o, "
                "CAST(:c AS rai.data_classification), :e) RETURNING project_id"
            ),
            {
                "n": body.name,
                "d": body.description,
                "o": owner,
                "c": body.data_classification,
                "e": body.default_environment_id,
            },
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
    nullable = ("description", "default_environment_id")
    cleared = sorted(k for k, v in changes.items() if v is None and k not in nullable)
    if cleared:
        raise Problem(
            422, "request.invalid", "Only description and default_environment_id may be null",
            detail=f"cannot clear: {', '.join(cleared)}",
        )  # fmt: skip
    if "owner_id" in changes:
        _check_owner(session.conn, changes["owner_id"])
    if changes.get("default_environment_id") is not None:
        check_environment(session.conn, changes["default_environment_id"])
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


# ---- declared inputs (lineage, B-19: FR-04.1) ------------------------------------------------


class InputIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_asset_id: UUID = Field(description="the asset this one is derived from")
    transform_ref: str | None = Field(
        default=None, min_length=1, max_length=300, description="e.g. etl-job@<commit SHA>"
    )


class InputOut(BaseModel):
    input_id: UUID
    asset_id: UUID
    input_asset_id: UUID
    input_name: str
    input_kind: AssetKind
    transform_ref: str | None
    created_by: UUID
    created_at: datetime


_INPUT = (
    "SELECT i.input_id, i.asset_id, i.input_asset_id, a.name AS input_name, "
    "a.kind::text AS input_kind, i.transform_ref, i.created_by, i.created_at "
    "FROM rai.asset_inputs i JOIN rai.pipeline_assets a ON a.asset_id = i.input_asset_id"
)


@router.get("/assets/{asset_id}/inputs")
def list_inputs(
    asset_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
) -> PageOut[InputOut]:
    """What this asset is derived from (one hop; a run pins every hop upstream)."""
    find_asset(session.conn, asset_id)
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_INPUT} WHERE i.asset_id = :a "
            "AND (CAST(:after AS uuid) IS NULL OR i.input_id > :after) "
            "ORDER BY i.input_id LIMIT :n"
        ),
        {"a": asset_id, "after": after, "n": wanted.limit + 1},
    ).all()
    inputs = [InputOut.model_validate(r._asdict()) for r in rows]
    return paged(inputs, wanted, lambda i: str(i.input_id))


@router.post("/assets/{asset_id}/inputs", status_code=201)
def declare_input(
    asset_id: UUID,
    body: InputIn,
    response: Response,
    session: Annotated[TenantSession, Depends(engineer)],
) -> InputOut:
    asset = find_asset(session.conn, asset_id)
    find_project(session.conn, asset.project_id, for_change=True)
    source = find_asset(session.conn, body.input_asset_id)
    if source.project_id != asset.project_id:
        raise Problem(422, "lineage.other_project", "Both assets must be in the same project")
    if source.asset_id == asset.asset_id:
        raise Problem(422, "lineage.cycle", "An asset cannot be derived from itself")
    try:
        with session.conn.begin_nested():
            input_id = session.conn.execute(
                text(
                    "INSERT INTO rai.asset_inputs (project_id, asset_id, input_asset_id, "
                    "transform_ref, created_by) VALUES (:p, :a, :i, :t, :u) RETURNING input_id"
                ),
                {
                    "p": asset.project_id, "a": asset_id, "i": body.input_asset_id,
                    "t": body.transform_ref, "u": session.user_id,
                },
            ).scalar_one()  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(
                409, "lineage.duplicate_input", "This input is already declared"
            ) from None
        if getattr(error.orig, "sqlstate", None) == CHECK_VIOLATION:
            raise Problem(
                422, "lineage.cycle", "This input would close a cycle",
                detail=f"{asset.name} already feeds {source.name}, directly or indirectly",
            ) from None  # fmt: skip
        raise
    response.headers["Location"] = f"/v1/assets/{asset_id}/inputs/{input_id}"
    row = session.conn.execute(text(f"{_INPUT} WHERE i.input_id = :i"), {"i": input_id}).one()
    return InputOut.model_validate(row._asdict())


@router.delete("/assets/{asset_id}/inputs/{input_id}", status_code=204)
def remove_input(
    asset_id: UUID, input_id: UUID, session: Annotated[TenantSession, Depends(engineer)]
) -> Response:
    """Remove a wrong declaration. Runs already queued keep the graph they pinned."""
    asset = find_asset(session.conn, asset_id)
    find_project(session.conn, asset.project_id, for_change=True)
    removed = session.conn.execute(
        text("DELETE FROM rai.asset_inputs WHERE input_id = :i AND asset_id = :a"),
        {"i": input_id, "a": asset_id},
    ).rowcount
    if not removed:
        raise Problem(404, "lineage.input_not_found", "Input not found")
    return Response(status_code=204)
