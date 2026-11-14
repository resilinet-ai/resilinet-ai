"""Who is calling, in which tenant, with which role (B-16: FR-01.1, NFR-SEC-1).

Every tenant-scoped request runs in one database transaction that first calls
``set_context`` (tenant from ``X-RAI-Tenant`` or the user's only tenant, actor
from the verified token, threat S-04). The role comes from the membership row,
read under row-level security, so a user who is not a member of the tenant gets
404 "tenant not found", never another tenant's data (threat I-07).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import Connection, Engine, text

from rai.api.problems import Problem
from rai.api.settings import Settings, SettingsError
from rai.api.tokens import InvalidToken, read_access_token
from rai.core.db import APP_ROLE
from rai.core.tenancy import set_context

Role = Literal["admin", "engineer", "reviewer", "viewer", "auditor"]
ROLES: tuple[Role, ...] = ("admin", "engineer", "reviewer", "viewer", "auditor")
TENANT_HEADER = "X-RAI-Tenant"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID


@dataclass(frozen=True, slots=True)
class TenantSession:
    conn: Connection
    tenant_id: UUID
    user_id: UUID
    role: Role


def engine_of(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@contextmanager
def app_transaction(engine: Engine) -> Iterator[Connection]:
    """A transaction as ``rai_app`` with no tenant: only the pre-tenant functions work."""
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        yield conn


def check_login_role(conn: Connection) -> None:
    """Refuse to serve if the API logged in with more power than ``rai_app`` (threat I-02)."""
    row = conn.execute(
        text(
            "SELECT r.rolsuper, r.rolbypassrls, "
            "pg_has_role(current_user, 'rai_app', 'MEMBER') AS in_app, "
            "EXISTS (SELECT FROM pg_namespace n WHERE n.nspname = 'rai' AND n.nspowner = r.oid) "
            "  OR EXISTS (SELECT FROM pg_tables t WHERE t.schemaname = 'rai' "
            "             AND t.tableowner = current_user) AS owns "
            "FROM pg_roles r WHERE r.rolname = current_user"
        )
    ).one()
    if row.rolsuper or row.rolbypassrls or row.owns or not row.in_app:
        raise SettingsError(
            "the API must log in as rai_api (a member of rai_app only), not as a superuser, "
            "an owner of the rai schema or a role that bypasses row-level security"
        )


def principal(request: Request) -> Principal:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    challenge = {"WWW-Authenticate": "Bearer"}
    if scheme.lower() != "bearer" or not token:
        raise Problem(401, "auth.required", "Sign in first", headers=challenge)
    try:
        return Principal(read_access_token(settings_of(request), token.strip()))
    except InvalidToken:
        raise Problem(
            401, "auth.invalid_token", "The access token is not valid or has expired",
            headers=challenge,
        ) from None  # fmt: skip


def _choose_tenant(conn: Connection, request: Request, user_id: UUID) -> UUID:
    header = request.headers.get(TENANT_HEADER)
    if header:
        try:
            return UUID(header)
        except ValueError:
            raise Problem(
                400, "tenant.invalid_header", f"{TENANT_HEADER} must be a tenant id (UUID)"
            ) from None
    tenants = conn.execute(text("SELECT tenant_id FROM rai.user_tenants(:u)"), {"u": user_id}).all()
    if len(tenants) == 1:
        return UUID(str(tenants[0].tenant_id))
    if not tenants:
        raise Problem(404, "tenant.not_found", "Tenant not found")
    raise Problem(400, "tenant.required", f"You belong to several tenants; send {TENANT_HEADER}")


def tenant_session(
    request: Request, caller: Annotated[Principal, Depends(principal)]
) -> Iterator[TenantSession]:
    with app_transaction(engine_of(request)) as conn:
        tenant = _choose_tenant(conn, request, caller.user_id)
        set_context(conn, tenant, "user", str(caller.user_id))
        role = conn.execute(
            text(
                "SELECT m.role FROM rai.memberships m JOIN rai.users u USING (user_id) "
                "JOIN rai.tenants t USING (tenant_id) "
                "WHERE m.user_id = :u AND u.status = 'active' AND t.status = 'active'"
            ),
            {"u": caller.user_id},
        ).scalar_one_or_none()
        if role is None:  # RLS hides other tenants: same answer as a tenant that does not exist
            raise Problem(404, "tenant.not_found", "Tenant not found")
        yield TenantSession(conn, tenant, caller.user_id, cast(Role, role))


def require(*roles: Role) -> Callable[[TenantSession], TenantSession]:
    """Dependency: the caller's role in the tenant must be one of ``roles`` (else 403)."""

    # scope="function": the transaction commits (or rolls back) when the endpoint
    # returns, before the response is sent, so a 2xx always means committed.
    def check(
        session: Annotated[TenantSession, Depends(tenant_session, scope="function")],
    ) -> TenantSession:
        if session.role not in roles:
            raise Problem(
                403, "auth.forbidden", "Your role cannot do this",
                detail=f"needs one of: {', '.join(roles)}",
            )  # fmt: skip
        return session

    return check
