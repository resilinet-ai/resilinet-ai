"""Database connection settings and Alembic wiring for the ``rai`` schema."""

from __future__ import annotations

import os

from alembic import command
from alembic.config import Config

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


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)
