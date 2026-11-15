"""Who is calling, in which tenant, with which role (B-16: FR-01.1, NFR-SEC-1).

Every tenant-scoped request runs in one database transaction that first calls
``set_context`` (tenant from ``X-RAI-Tenant`` or the user's only tenant, actor
from the verified token, threat S-04). The role comes from the membership row,
read under row-level security, so a user who is not a member of the tenant gets
404 "tenant not found", never another tenant's data (threat I-07).

**API keys (B-20, threat S-02).** A request may instead carry a project-scoped API
key (``Bearer rai_<prefix>_<secret>``). Its tenant and project come from the key,
it acts as actor ``api_key``, and it can only use endpoints that name one of its
scopes, for its own project; everything else is 403 (or 404 for other projects).
Each key is limited to ``key_requests_per_minute`` requests (threat D-03).
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
from rai.api.ratelimit import RequestLimiter
from rai.api.settings import Settings, SettingsError
from rai.api.tokens import InvalidToken, read_access_token
from rai.core import apikeys
from rai.core.db import APP_ROLE, login_role_problem
from rai.core.tenancy import set_context

Role = Literal["admin", "engineer", "reviewer", "viewer", "auditor"]
ROLES: tuple[Role, ...] = ("admin", "engineer", "reviewer", "viewer", "auditor")
TENANT_HEADER = "X-RAI-Tenant"


@dataclass(frozen=True, slots=True)
class KeyGrant:
    """What a verified API key may do."""

    api_key_id: UUID
    tenant_id: UUID
    project_id: UUID
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID  # for an API key: the user who created it (accountable for its runs)
    key: KeyGrant | None = None


@dataclass(frozen=True, slots=True)
class TenantSession:
    conn: Connection
    tenant_id: UUID
    user_id: UUID
    role: Role | None  # None for an API key: it has scopes, not a role
    key: KeyGrant | None = None

    def own_project(self, project_id: UUID) -> bool:
        """Whether this caller may see the project's runs (a key: only its own project)."""
        return self.key is None or self.key.project_id == project_id


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
    problem = login_role_problem(conn, "rai_api")
    if problem is not None:
        raise SettingsError(f"the API must {problem}" if problem.startswith("log in") else problem)


CHALLENGE = {"WWW-Authenticate": "Bearer"}


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Problem(401, "auth.required", "Sign in first", headers=CHALLENGE)
    return token.strip()


def _key_principal(request: Request, token: str) -> Principal:
    invalid = Problem(
        401, "auth.invalid_key", "The API key is not valid, has expired or was revoked",
        headers=CHALLENGE,
    )  # fmt: skip
    parsed = apikeys.parse(token)
    if parsed is None:
        raise invalid
    prefix, secret = parsed
    with app_transaction(engine_of(request)) as conn:
        row = conn.execute(
            text("SELECT * FROM rai.api_key_lookup(:p)"), {"p": prefix}
        ).one_or_none()
    if row is None or not apikeys.matches(secret, row.key_hash):
        raise invalid
    limiter: RequestLimiter = request.app.state.key_requests
    wait = limiter.take(prefix)
    if wait is not None:
        raise Problem(
            429, "auth.key_rate_limited", "Too many requests with this API key",
            headers={"Retry-After": str(wait)},
        )  # fmt: skip
    grant = KeyGrant(row.api_key_id, row.tenant_id, row.project_id, frozenset(row.scopes))
    return Principal(row.created_by, grant)


def caller(request: Request) -> Principal:
    """A signed-in user or a project API key."""
    token = _bearer(request)
    if apikeys.is_key(token):
        return _key_principal(request, token)
    return principal(request)


def principal(request: Request) -> Principal:
    """A signed-in user only: API keys cannot manage accounts, sessions or keys."""
    token = _bearer(request)
    if apikeys.is_key(token):
        raise Problem(403, "auth.key_not_allowed", "API keys cannot be used here")
    try:
        return Principal(read_access_token(settings_of(request), token))
    except InvalidToken:
        raise Problem(
            401, "auth.invalid_token", "The access token is not valid or has expired",
            headers=CHALLENGE,
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


def _key_session(
    request: Request, conn: Connection, grant: KeyGrant, user_id: UUID
) -> TenantSession:
    header = request.headers.get(TENANT_HEADER)
    if header and header.strip().lower() != str(grant.tenant_id):
        raise Problem(404, "tenant.not_found", "Tenant not found")
    set_context(conn, grant.tenant_id, "api_key", str(grant.api_key_id))
    return TenantSession(conn, grant.tenant_id, user_id, None, grant)


def tenant_session(
    request: Request, who: Annotated[Principal, Depends(caller)]
) -> Iterator[TenantSession]:
    with app_transaction(engine_of(request)) as conn:
        if who.key is not None:
            yield _key_session(request, conn, who.key, who.user_id)
            return
        tenant = _choose_tenant(conn, request, who.user_id)
        set_context(conn, tenant, "user", str(who.user_id))
        role = conn.execute(
            text(
                "SELECT m.role FROM rai.memberships m JOIN rai.users u USING (user_id) "
                "JOIN rai.tenants t USING (tenant_id) "
                "WHERE m.user_id = :u AND u.status = 'active' AND t.status = 'active'"
            ),
            {"u": who.user_id},
        ).scalar_one_or_none()
        if role is None:  # RLS hides other tenants: same answer as a tenant that does not exist
            raise Problem(404, "tenant.not_found", "Tenant not found")
        yield TenantSession(conn, tenant, who.user_id, cast(Role, role))


def require(
    *roles: Role, key_scope: apikeys.Scope | None = None
) -> Callable[[TenantSession], TenantSession]:
    """Dependency: the caller's role in the tenant must be one of ``roles`` (else 403).
    An API key passes only if the endpoint names ``key_scope`` and the key has it."""

    # scope="function": the transaction commits (or rolls back) when the endpoint
    # returns, before the response is sent, so a 2xx always means committed.
    def check(
        session: Annotated[TenantSession, Depends(tenant_session, scope="function")],
    ) -> TenantSession:
        if session.key is not None:
            if key_scope is None or key_scope not in session.key.scopes:
                raise Problem(
                    403, "auth.key_scope", "This API key cannot do this",
                    detail=f"needs scope {key_scope}" if key_scope else "users only",
                )  # fmt: skip
            return session
        if session.role not in roles:
            raise Problem(
                403, "auth.forbidden", "Your role cannot do this",
                detail=f"needs one of: {', '.join(roles)}",
            )  # fmt: skip
        return session

    return check
