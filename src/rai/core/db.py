"""Database connection settings and Alembic wiring for the ``rai`` schema."""

from __future__ import annotations

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

DATABASE_URL_ENV = "RAI_DATABASE_URL"
APP_ROLE = "rai_app"


def sqlalchemy_url(url: str) -> str:
    """Use the psycopg 3 driver for plain ``postgresql://`` URLs."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise RuntimeError(f"set {DATABASE_URL_ENV} to the rai-db connection string")
    return sqlalchemy_url(url)


def alembic_config(url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", "rai:migrations")
    resolved = sqlalchemy_url(url) if url else database_url()
    config.set_main_option("sqlalchemy.url", resolved.replace("%", "%%"))  # configparser escape
    return config


def login_role_problem(conn: Connection, expected: str) -> str | None:
    """Why this connection must not serve the API or worker, or None (threat I-02).

    The service must log in as ``expected`` (rai_api or rai_worker): a member of
    rai_app, not a superuser, not an owner of the rai schema, not BYPASSRLS.
    """
    row = conn.execute(
        text(
            # Roles are cluster-wide; the schema says whether this database is migrated.
            "SELECT to_regnamespace('rai') IS NOT NULL AND to_regrole('rai_app') IS NOT NULL "
            "AS migrated, current_user AS name, r.rolsuper, r.rolbypassrls, "
            "COALESCE(pg_has_role(current_user, to_regrole('rai_app'), 'MEMBER'), false) "
            "AS in_app, "
            "EXISTS (SELECT FROM pg_namespace n WHERE n.nspname = 'rai' AND n.nspowner = r.oid) "
            "  OR EXISTS (SELECT FROM pg_tables t WHERE t.schemaname = 'rai' "
            "             AND t.tableowner = current_user) AS owns "
            "FROM pg_roles r WHERE r.rolname = current_user"
        )
    ).one()
    if not row.migrated:
        return "the rai schema is not set up; run `rai db upgrade` first"
    if row.rolsuper or row.rolbypassrls or row.owns or not row.in_app or row.name != expected:
        return (
            f"log in as {expected} (a member of rai_app only), not as a superuser, "
            "an owner of the rai schema or a role that bypasses row-level security"
        )
    return None


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)
