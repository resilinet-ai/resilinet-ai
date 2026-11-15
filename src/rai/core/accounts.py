"""Operator account tasks that need the schema owner (B-16: FR-01.1).

The API's role cannot create tenants (threat E-04) or change other users'
passwords, so these run from ``rai admin`` with ``RAI_DATABASE_URL`` (the owner),
on the database host's operator. Each change is attributed to a ``system`` actor
in the audit chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import sql
from sqlalchemy import Connection, text

API_ROLE = "rai_api"


@dataclass(frozen=True, slots=True)
class NewTenant:
    tenant_id: UUID
    admin_user_id: UUID
    admin_created: bool  # False: an existing account was made admin, its password untouched


def _system_actor(conn: Connection, task: str) -> None:
    conn.execute(
        text(
            "SELECT set_config('app.actor_type', 'system', true), "
            "set_config('app.actor_id', :a, true)"
        ),
        {"a": f"rai-cli:{task}"},
    )


def create_tenant(
    conn: Connection,
    *,
    name: str,
    slug: str,
    admin_email: str,
    admin_name: str,
    password_hash: str | None,
) -> NewTenant:
    """Create a tenant and its first admin (a new account, or an existing one)."""
    _system_actor(conn, "create-tenant")
    tenant = conn.execute(
        text("INSERT INTO rai.tenants (name, slug) VALUES (:n, :s) RETURNING tenant_id"),
        {"n": name, "s": slug},
    ).scalar_one()
    created = conn.execute(
        text(
            "INSERT INTO rai.users (email, display_name, password_hash, status) "
            "VALUES (:e, :d, CAST(:h AS text), CASE WHEN CAST(:h AS text) IS NULL "
            "THEN 'invited' ELSE 'active' END::rai.user_status) "
            "ON CONFLICT (email) DO NOTHING RETURNING user_id"
        ),
        {"e": admin_email, "d": admin_name, "h": password_hash},
    ).scalar_one_or_none()
    user = (
        created
        or conn.execute(
            text("SELECT user_id FROM rai.users WHERE email = :e"), {"e": admin_email}
        ).scalar_one()
    )
    conn.execute(
        text("INSERT INTO rai.memberships (tenant_id, user_id, role) VALUES (:t, :u, 'admin')"),
        {"t": tenant, "u": user},
    )
    return NewTenant(UUID(str(tenant)), UUID(str(user)), created is not None)


def set_password(conn: Connection, *, email: str, password_hash: str) -> bool:
    """Set a password, activate an invited account and end all its sessions.
    A disabled account stays disabled. False if there is no such account."""
    _system_actor(conn, "set-password")
    user = conn.execute(
        text(
            "UPDATE rai.users SET password_hash = :h, status = CASE status "
            "WHEN 'invited' THEN 'active'::rai.user_status ELSE status END "
            "WHERE email = :e RETURNING user_id"
        ),
        {"h": password_hash, "e": email},
    ).scalar_one_or_none()
    if user is None:
        return False
    conn.execute(
        text(
            "UPDATE rai.refresh_tokens SET revoked_at = clock_timestamp() "
            "WHERE user_id = :u AND revoked_at IS NULL"
        ),
        {"u": user},
    )
    return True


LOGIN_ROLES = (API_ROLE, "rai_worker")


def set_role_password(conn: Connection, role: str, password: str) -> None:
    """ALTER ROLE needs a literal, so the password is quoted by psycopg, never formatted.
    Only the service login roles can be changed this way."""
    if role not in LOGIN_ROLES:
        raise ValueError(f"role must be one of {', '.join(LOGIN_ROLES)}")
    raw = conn.connection.driver_connection
    statement = sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
        sql.Identifier(role), sql.Literal(password)
    )
    conn.exec_driver_sql(statement.as_string(raw))


def set_api_role_password(conn: Connection, password: str) -> None:
    set_role_password(conn, API_ROLE, password)
