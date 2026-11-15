"""Project-scoped API keys for CI (B-20: FR-06.1; threat S-02).

Admins and engineers create, list and revoke keys; keys themselves cannot. The
secret is in the creation response only (``Cache-Control: no-store``); the
database keeps its SHA-256. ``expires_in_days`` is required in effect: it
defaults to 30 and may not exceed 90.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from rai.api.context import TenantSession, require
from rai.api.listing import Page, PageOut, after_uuid, page, paged
from rai.api.problems import Problem
from rai.api.routes_projects import find_project
from rai.core import apikeys

router = APIRouter(prefix="/v1/api-keys", tags=["api keys"])

manager = require("admin", "engineer")


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    name: str = Field(min_length=1, max_length=100, description="e.g. github-actions")
    scopes: list[apikeys.Scope] = Field(default=list(apikeys.SCOPES), min_length=1)
    expires_in_days: int = Field(default=30, ge=1, le=apikeys.MAX_DAYS)


class KeyOut(BaseModel):
    api_key_id: UUID
    project_id: UUID
    name: str
    prefix: str
    scopes: list[apikeys.Scope]
    state: Literal["active", "expired", "revoked"]
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class KeyCreated(KeyOut):
    key: str = Field(description="shown once; store it as a CI secret")


_KEY = (
    "SELECT api_key_id, project_id, name, key_prefix AS prefix, scopes::text[] AS scopes, "
    "CASE WHEN revoked_at IS NOT NULL THEN 'revoked' "
    "WHEN expires_at <= clock_timestamp() THEN 'expired' ELSE 'active' END AS state, "
    "created_by, created_at, expires_at, revoked_at FROM rai.api_keys"
)


def _find(conn: Connection, api_key_id: UUID) -> KeyOut:
    row = conn.execute(text(f"{_KEY} WHERE api_key_id = :k"), {"k": api_key_id}).first()
    if row is None:
        raise Problem(404, "api_keys.not_found", "API key not found")
    return KeyOut.model_validate(row._asdict())


@router.post("", status_code=201)
def create_key(
    body: KeyIn, response: Response, session: Annotated[TenantSession, Depends(manager)]
) -> KeyCreated:
    find_project(session.conn, body.project_id, for_change=True)
    new = apikeys.generate()
    api_key_id = session.conn.execute(
        text(
            "WITH now AS (SELECT clock_timestamp() AS t) "
            "INSERT INTO rai.api_keys (project_id, name, key_prefix, key_hash, scopes, "
            "created_by, created_at, expires_at) "
            "SELECT :p, :n, :prefix, :h, CAST(:s AS rai.api_key_scope[]), :u, t, "
            "t + make_interval(days => :d) FROM now RETURNING api_key_id"
        ),
        {
            "p": body.project_id, "n": body.name, "prefix": new.prefix, "h": new.secret_hash,
            "s": sorted(set(body.scopes)), "u": session.user_id, "d": body.expires_in_days,
        },
    ).scalar_one()  # fmt: skip
    response.headers["Location"] = f"/v1/api-keys/{api_key_id}"
    return KeyCreated(**_find(session.conn, api_key_id).model_dump(), key=new.token)


@router.get("")
def list_keys(
    session: Annotated[TenantSession, Depends(manager)],
    wanted: Annotated[Page, Depends(page)],
    project_id: Annotated[UUID | None, Query()] = None,
) -> PageOut[KeyOut]:
    """Newest first. Never includes secrets or hashes."""
    after = after_uuid(wanted)
    rows = session.conn.execute(
        text(
            f"{_KEY} WHERE (CAST(:p AS uuid) IS NULL OR project_id = :p) "
            "AND (CAST(:after AS uuid) IS NULL OR api_key_id < :after) "
            "ORDER BY api_key_id DESC LIMIT :n"
        ),
        {"p": project_id, "after": after, "n": wanted.limit + 1},
    ).all()
    keys = [KeyOut.model_validate(r._asdict()) for r in rows]
    return paged(keys, wanted, lambda k: str(k.api_key_id))


@router.get("/{api_key_id}")
def get_key(api_key_id: UUID, session: Annotated[TenantSession, Depends(manager)]) -> KeyOut:
    return _find(session.conn, api_key_id)


@router.delete("/{api_key_id}", status_code=204)
def revoke_key(api_key_id: UUID, session: Annotated[TenantSession, Depends(manager)]) -> Response:
    """Revoke at once. Revoking a revoked key again changes nothing."""
    _find(session.conn, api_key_id)
    session.conn.execute(
        text(
            "UPDATE rai.api_keys SET revoked_at = clock_timestamp(), revoked_by = :u "
            "WHERE api_key_id = :k AND revoked_at IS NULL"
        ),
        {"k": api_key_id, "u": session.user_id},
    )
    return Response(status_code=204)
