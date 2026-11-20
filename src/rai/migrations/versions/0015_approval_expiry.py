"""Scenario approvals expire (B-43: FR-07.2, NFR-SAF-1).

A reviewer's approval vouches for a scenario against the system as it was when they
looked. It should not run that scenario months later. Every approval now carries an
``expires_at``, in the future and at most 90 days after it was given (the API's
default is 30 days); a rejection carries none.

* **Existing approvals are not rewritten.** ``scenario_approvals`` is append-only, so
  approvals given before this migration keep a NULL expiry, and RAI treats an approval
  without an expiry as expired: those scenarios need a fresh review before they run
  again. That fails closed. The checks are ``NOT VALID`` for the same reason: they
  hold for every new row without touching the old ones.
* Once an approval expires, queuing a run refuses it, the worker refuses it at each
  fault, and the chaos agent refuses it on its own from the signed request.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-20
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE rai.scenario_approvals ADD COLUMN expires_at timestamptz;
ALTER TABLE rai.scenario_approvals
    ADD CONSTRAINT scenario_approvals_expiry
    CHECK ((decision = 'approve') = (expires_at IS NOT NULL)) NOT VALID,
    ADD CONSTRAINT scenario_approvals_expiry_range
    CHECK (expires_at IS NULL
           OR (expires_at > created_at AND expires_at <= created_at + interval '90 days'))
    NOT VALID;
"""

DOWNGRADE = """
ALTER TABLE rai.scenario_approvals
    DROP CONSTRAINT scenario_approvals_expiry_range,
    DROP CONSTRAINT scenario_approvals_expiry,
    DROP COLUMN expires_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
