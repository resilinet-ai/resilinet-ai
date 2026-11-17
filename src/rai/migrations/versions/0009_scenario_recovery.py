"""How a scenario measures recovery (B-26b: FR-08.1, FR-08.2).

``chaos_scenarios.recovery`` holds the scenario's recovery plan (probes, the ledger
to measure RPO on, and the RTO and RPO objectives); null for a scenario that only
injects faults. Scenario versions stay immutable: the column is set when a version
is published and never changed.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE rai.chaos_scenarios ADD COLUMN recovery jsonb
    CHECK (recovery IS NULL OR jsonb_typeof(recovery) = 'object');
"""

DOWNGRADE = """
ALTER TABLE rai.chaos_scenarios DROP COLUMN recovery;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
