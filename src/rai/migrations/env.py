"""Alembic environment: online migrations only, URL from the config or RAI_DATABASE_URL."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from rai.core.db import database_url

config = context.config
url = config.get_main_option("sqlalchemy.url") or database_url()

engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None, version_table_schema="public")
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
