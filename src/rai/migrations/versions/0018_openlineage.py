"""OpenLineage ingest (FR-04.2): an external orchestrator's jobs in a project's lineage.

* ``openlineage_datasets`` maps an OpenLineage dataset (``namespace``, ``name``) to a
  registered asset of the project, so an orchestrator's job connects to the assets
  RAI tests. One dataset per asset and one asset per dataset in a project; removable
  (audited), never updated.
* ``openlineage_events`` keeps the core of every run event an orchestrator posts: the
  event type and time, the run, the job, and the input and output datasets. Facets are
  **not** kept, only their names: they can carry SQL, paths or environment details, and
  RAI only needs the graph. Insert-only and audited; the same event posted twice is
  stored once (``event_sha256``), since OpenLineage clients retry.
* ``api_key_scope`` gains ``lineage:write``, for an orchestrator's project API key.

The downgrade drops both tables and leaves the enum value (see migration 0013).

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-21
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE rai.openlineage_datasets (
    mapping_id  uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id   uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id  uuid NOT NULL,
    asset_id    uuid NOT NULL,
    namespace   text NOT NULL CHECK (length(namespace) BETWEEN 1 AND 500),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 500),
    created_by  uuid NOT NULL REFERENCES rai.users (user_id),
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, namespace, name),
    UNIQUE (asset_id),
    FOREIGN KEY (tenant_id, project_id, asset_id)
        REFERENCES rai.pipeline_assets (tenant_id, project_id, asset_id)
);
CREATE TRIGGER openlineage_datasets_no_update BEFORE UPDATE ON rai.openlineage_datasets
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER openlineage_datasets_audit AFTER INSERT OR DELETE ON rai.openlineage_datasets
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('mapping_id', 'openlineage_dataset');

CREATE TABLE rai.openlineage_events (
    event_id       uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id      uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id     uuid NOT NULL,
    event_sha256   char(64) NOT NULL CHECK (event_sha256 ~ '^[0-9a-f]{64}$'),
    event_type     text NOT NULL
        CHECK (event_type IN ('START', 'RUNNING', 'COMPLETE', 'FAIL', 'ABORT', 'OTHER')),
    event_time     timestamptz NOT NULL,
    run_id         uuid NOT NULL,  -- the orchestrator's run, not a RAI run
    job_namespace  text NOT NULL CHECK (length(job_namespace) BETWEEN 1 AND 500),
    job_name       text NOT NULL CHECK (length(job_name) BETWEEN 1 AND 500),
    inputs         jsonb NOT NULL CHECK (jsonb_typeof(inputs) = 'array'),
    outputs        jsonb NOT NULL CHECK (jsonb_typeof(outputs) = 'array'),
    facet_names    text[] NOT NULL DEFAULT '{}',
    producer       text NOT NULL CHECK (length(producer) BETWEEN 1 AND 500),
    received_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, event_sha256),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES rai.assurance_projects (tenant_id, project_id)
);
CREATE INDEX openlineage_events_job_idx
    ON rai.openlineage_events (project_id, job_namespace, job_name, event_time);
CREATE TRIGGER openlineage_events_immutable BEFORE UPDATE OR DELETE ON rai.openlineage_events
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER openlineage_events_no_truncate BEFORE TRUNCATE ON rai.openlineage_events
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER openlineage_events_audit AFTER INSERT ON rai.openlineage_events
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('event_id', 'openlineage_event');
"""

for _table in ("openlineage_datasets", "openlineage_events"):
    UPGRADE += f"""
ALTER TABLE rai.{_table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.{_table} TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
"""

UPGRADE += """
GRANT SELECT, INSERT, DELETE ON rai.openlineage_datasets TO rai_app;
GRANT SELECT, INSERT ON rai.openlineage_events TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.openlineage_events, rai.openlineage_datasets;
"""


def upgrade() -> None:
    op.execute("ALTER TYPE rai.api_key_scope ADD VALUE IF NOT EXISTS 'lineage:write'")
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
