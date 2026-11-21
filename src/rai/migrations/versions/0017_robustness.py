"""Robustness tests (FR-09.3): the ``robustness_check`` test type, and scoring targets.

A ``robustness_check`` reads a feature table, perturbs it itself (seeded and recorded)
and has each variant scored by the system under test's model, through a target on the
environment's allow-list. RAI never runs the model (decision TEVV-METRIC).

* ``rai.test_type`` gains ``robustness_check``. The definition is checked by the worker.
* ``environment_targets.score_url`` is where an ``http`` target scores rows. Only an
  ``http`` target has one; the worker posts nowhere else.

The downgrade drops the column and leaves the enum value in place, as migration 0013
explains: PostgreSQL cannot drop an enum value, and an unused one is harmless.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-21
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE rai.environment_targets
    ADD COLUMN score_url text
        CHECK (score_url ~ '^https?://\\S+$' AND length(score_url) <= 500),
    ADD CONSTRAINT environment_targets_score_on_http
        CHECK (score_url IS NULL OR kind = 'http');
"""

DOWNGRADE = """
ALTER TABLE rai.environment_targets
    DROP CONSTRAINT environment_targets_score_on_http,
    DROP COLUMN score_url;
"""


def upgrade() -> None:
    op.execute("ALTER TYPE rai.test_type ADD VALUE IF NOT EXISTS 'robustness_check'")
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
