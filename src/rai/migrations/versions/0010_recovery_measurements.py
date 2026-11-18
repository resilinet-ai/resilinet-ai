"""Append-only recovery measurements (B-29: FR-08.1, FR-08.2, NFR-AUD-1; threat T-03).

``recovery_measurements`` holds one row per scenario step that measured recovery:
onset, revert and restoration times, RTO and RPO with the objectives they were held
to, and whether each was met. It is written in the same transaction as the step's
``run_steps`` row (a foreign key to it), so a measurement exists exactly when its
step was recorded, and it is insert-only like observations: a trigger refuses UPDATE,
DELETE and TRUNCATE, even from the owner. A correction is a new run, never an edit.

``rto_met`` and ``rpo_met`` are null when the scenario set no objective, so summaries
count them only against measurements that had one. A step that was never restored,
or whose ledger could not be read after recovery, is a miss with a null value, not a
gap: ``rpo_attempted`` says the scenario names a ledger, whether or not it answered.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UPGRADE = """
CREATE TABLE rai.recovery_measurements (
    measurement_id        uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id             uuid NOT NULL DEFAULT rai.current_tenant(),
    run_id                uuid NOT NULL,
    step_key              text NOT NULL,
    scenario_id           uuid NOT NULL,
    method                text NOT NULL DEFAULT 'recovery-1'
                          CHECK (method ~ '^[a-z0-9.-]{1,40}$'),
    onset_at              timestamptz NOT NULL,
    reverted_at           timestamptz NOT NULL CHECK (reverted_at >= onset_at),
    restored_at           timestamptz CHECK (restored_at >= onset_at),
    rto_seconds           numeric(12, 3) CHECK (rto_seconds >= 0),
    rto_objective_seconds integer CHECK (rto_objective_seconds >= 0),
    rto_met               boolean,
    rpo_attempted         boolean NOT NULL,
    rpo_seconds           numeric(12, 3) CHECK (rpo_seconds >= 0),
    rpo_objective_seconds integer CHECK (rpo_objective_seconds >= 0),
    rpo_met               boolean,
    lost_rows             bigint CHECK (lost_rows >= 0),
    stable_checks         integer NOT NULL CHECK (stable_checks >= 1),
    window_seconds        integer NOT NULL CHECK (window_seconds >= 1),
    recorded_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, step_key),
    CHECK ((restored_at IS NULL) = (rto_seconds IS NULL)),
    CHECK ((rto_objective_seconds IS NULL) = (rto_met IS NULL)),
    CHECK (rpo_attempted OR (rpo_seconds IS NULL AND lost_rows IS NULL AND rpo_met IS NULL)),
    CHECK ((rpo_objective_seconds IS NULL) = (rpo_met IS NULL)),
    FOREIGN KEY (tenant_id, run_id) REFERENCES rai.runs (tenant_id, run_id),
    FOREIGN KEY (run_id, step_key) REFERENCES rai.run_steps (run_id, step_key),
    FOREIGN KEY (tenant_id, scenario_id) REFERENCES rai.chaos_scenarios (tenant_id, scenario_id)
);
CREATE INDEX recovery_measurements_scenario_idx
    ON rai.recovery_measurements (scenario_id, onset_at DESC);
CREATE INDEX recovery_measurements_onset_idx ON rai.recovery_measurements (onset_at DESC);

CREATE TRIGGER recovery_measurements_immutable BEFORE UPDATE OR DELETE
    ON rai.recovery_measurements FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER recovery_measurements_no_truncate BEFORE TRUNCATE
    ON rai.recovery_measurements FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();

ALTER TABLE rai.recovery_measurements ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.recovery_measurements TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
GRANT SELECT, INSERT ON rai.recovery_measurements TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.recovery_measurements;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
