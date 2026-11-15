"""Declared asset inputs and run lineage edges with digests (B-19: FR-04.1).

* ``asset_inputs`` declare how a project's pipeline fits together: asset X is
  derived from asset Y, optionally through a named transform (``transform_ref``,
  e.g. a job name and commit). Both assets are in the same project; a declaration
  that would close a cycle is refused (trigger, serialised per project). Engineers
  may remove a wrong declaration; runs are unaffected, because each run pins the
  graph it was queued with. Inserts and deletes are audited.
* ``lineage_edges`` are what a run recorded: for each step, every hop of the
  target's pinned upstream graph, the target into the run, and each reference
  dataset into the run, with the ``rai-frame-digest/1`` SHA-256 of the data the
  worker actually read. They are written in the step's transaction (after its
  ``run_steps`` row), so they exist exactly when the step does. Insert-only.

Data Model section 4 lists ``lineage_edges`` without ``step_key``, ``from_name``
and ``from_rows``; they are added so a reference dataset (not a registered asset)
can be an input, and so each edge names the step that read it (decision log
RAI-LINEAGE).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE rai.asset_inputs (
    input_id       uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id      uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id     uuid NOT NULL,
    asset_id       uuid NOT NULL,  -- the derived asset
    input_asset_id uuid NOT NULL,  -- what it is derived from
    transform_ref  text CHECK (length(transform_ref) BETWEEN 1 AND 300),
    created_by     uuid NOT NULL REFERENCES rai.users (user_id),
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (asset_id, input_asset_id),
    CHECK (asset_id <> input_asset_id),
    FOREIGN KEY (tenant_id, project_id, asset_id)
        REFERENCES rai.pipeline_assets (tenant_id, project_id, asset_id),
    FOREIGN KEY (tenant_id, project_id, input_asset_id)
        REFERENCES rai.pipeline_assets (tenant_id, project_id, asset_id)
);
CREATE INDEX asset_inputs_asset_idx ON rai.asset_inputs (tenant_id, asset_id);

-- A declaration may not close a cycle. Declarations in one project are serialised
-- by an advisory lock, so two concurrent declarations cannot close one together.
CREATE FUNCTION rai.asset_inputs_acyclic() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('rai.asset_inputs:' || NEW.project_id, 0));
    IF EXISTS (
        WITH RECURSIVE upstream (asset_id) AS (
            SELECT NEW.input_asset_id
            UNION
            SELECT i.input_asset_id FROM rai.asset_inputs i
            JOIN upstream u ON i.asset_id = u.asset_id
        )
        SELECT FROM upstream WHERE asset_id = NEW.asset_id
    ) THEN
        RAISE EXCEPTION 'asset % already feeds asset %; this input would close a cycle',
            NEW.asset_id, NEW.input_asset_id
            USING ERRCODE = 'check_violation', CONSTRAINT = 'asset_inputs_acyclic';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER asset_inputs_acyclic BEFORE INSERT ON rai.asset_inputs
    FOR EACH ROW EXECUTE FUNCTION rai.asset_inputs_acyclic();
CREATE TRIGGER asset_inputs_no_update BEFORE UPDATE ON rai.asset_inputs
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER asset_inputs_audit AFTER INSERT OR DELETE ON rai.asset_inputs
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('input_id', 'asset_input');

CREATE TABLE rai.lineage_edges (
    edge_id       uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id     uuid NOT NULL DEFAULT rai.current_tenant(),
    run_id        uuid NOT NULL,
    step_key      text NOT NULL,
    from_asset_id uuid,           -- null: a reference dataset, not a registered asset
    from_name     text NOT NULL CHECK (length(from_name) BETWEEN 1 AND 200),
    to_asset_id   uuid,           -- null: the run itself
    from_digest   char(64) CHECK (from_digest ~ '^[0-9a-f]{64}$'),
    to_digest     char(64) CHECK (to_digest ~ '^[0-9a-f]{64}$'),
    from_rows     bigint CHECK (from_rows >= 0),
    transform_ref text CHECK (length(transform_ref) <= 300),
    recorded_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE NULLS NOT DISTINCT (run_id, step_key, from_name, to_asset_id),
    CHECK ((from_digest IS NULL) = (from_rows IS NULL)),
    FOREIGN KEY (tenant_id, run_id) REFERENCES rai.runs (tenant_id, run_id),
    FOREIGN KEY (run_id, step_key) REFERENCES rai.run_steps (run_id, step_key),
    FOREIGN KEY (tenant_id, from_asset_id) REFERENCES rai.pipeline_assets (tenant_id, asset_id),
    FOREIGN KEY (tenant_id, to_asset_id) REFERENCES rai.pipeline_assets (tenant_id, asset_id)
);
CREATE INDEX lineage_edges_run_idx ON rai.lineage_edges (run_id, edge_id);
CREATE TRIGGER lineage_edges_immutable BEFORE UPDATE OR DELETE ON rai.lineage_edges
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER lineage_edges_no_truncate BEFORE TRUNCATE ON rai.lineage_edges
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
"""

for _table in ("asset_inputs", "lineage_edges"):
    UPGRADE += f"""
ALTER TABLE rai.{_table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.{_table} TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
"""

UPGRADE += """
GRANT SELECT, INSERT, DELETE ON rai.asset_inputs TO rai_app;
GRANT SELECT, INSERT ON rai.lineage_edges TO rai_app;
GRANT EXECUTE ON FUNCTION rai.asset_inputs_acyclic() TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.lineage_edges, rai.asset_inputs;
DROP FUNCTION rai.asset_inputs_acyclic();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
