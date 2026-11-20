"""The privacy_canary test type (FR-05.1): the synthetic canary privacy sentinel.

A ``privacy_canary`` test case searches its target asset for canary values listed in
an answer key (another asset of the project, pinned with the run like a drift check's
reference). Only the enum gains a value; the definition is checked by the worker.

The downgrade leaves the value in place. PostgreSQL cannot drop an enum value, and
rebuilding the type would mean rewriting test cases, which are immutable. An unused
value is harmless, the upgrade is idempotent (``IF NOT EXISTS``), and migration 0003's
downgrade drops the whole type.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-19
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE rai.test_type ADD VALUE IF NOT EXISTS 'privacy_canary'")


def downgrade() -> None:
    """Nothing to undo safely; see the module docstring."""
