"""Projects and test definitions (B-17: FR-01.1, FR-03.1, FR-03.4, FR-04.1, FR-06.1).

Data Model section 3. What a run will test, stored so a run can always be replayed:

* ``assurance_projects`` is the one mutable table here (name, owner, status...);
  ``row_version`` backs the API's ETag / If-Match.
* ``pipeline_assets`` are registered once (insert-only for the API).
* ``data_contracts``, ``test_cases``, ``test_plans`` and ``plan_items`` are versioned
  and immutable: the app role has no UPDATE or DELETE grant, and a trigger refuses
  either anyway. Editing means publishing the next version.
* Every row carries ``tenant_id``, under row-level security. Foreign keys include
  ``tenant_id`` (and ``project_id`` where it matters), because PostgreSQL checks
  foreign keys without RLS: a plain key could point at another tenant's row.
* Every insert and update is written to the audit chain by the existing trigger.

Columns for things that arrive later (environments B-22, connectors, scenarios
B-26) exist now without foreign keys; those migrations add the keys.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = (
    "assurance_projects",
    "pipeline_assets",
    "data_contracts",
    "test_cases",
    "test_plans",
    "plan_items",
)
IMMUTABLE = ("data_contracts", "test_cases", "test_plans", "plan_items")

UPGRADE = r"""
CREATE TYPE rai.data_classification AS ENUM ('synthetic', 'authorized_snapshot');
CREATE TYPE rai.project_status AS ENUM ('active', 'archived');
CREATE TYPE rai.asset_kind AS ENUM ('source', 'table', 'transform', 'feature_set', 'model');
CREATE TYPE rai.test_type AS ENUM
    ('contract_rule', 'recovery_assertion', 'consistency_check', 'tevv_metric', 'drift_check');
CREATE TYPE rai.severity AS ENUM ('info', 'warn', 'critical');
CREATE TYPE rai.plan_trigger AS ENUM ('manual', 'ci', 'schedule');
CREATE TYPE rai.plan_item_type AS ENUM ('test_case', 'scenario');

CREATE TABLE rai.assurance_projects (
    project_id             uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id              uuid NOT NULL DEFAULT rai.current_tenant()
                           REFERENCES rai.tenants (tenant_id),
    name                   text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    description            text CHECK (length(description) <= 2000),
    owner_id               uuid NOT NULL REFERENCES rai.users (user_id),
    data_classification    rai.data_classification NOT NULL DEFAULT 'synthetic',
    default_environment_id uuid,  -- foreign key added with environments (B-22)
    status                 rai.project_status NOT NULL DEFAULT 'active',
    row_version            integer NOT NULL DEFAULT 1,
    created_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, project_id),
    UNIQUE (tenant_id, name)
);

CREATE TABLE rai.pipeline_assets (
    asset_id       uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id      uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id     uuid NOT NULL,
    kind           rai.asset_kind NOT NULL,
    name           text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,62}$'),
    connector_id   uuid,  -- foreign key added with the connectors table
    locator        text CHECK (length(locator) BETWEEN 1 AND 500),
    schema_version integer CHECK (schema_version >= 1),
    created_by     uuid NOT NULL REFERENCES rai.users (user_id),
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, name),
    UNIQUE (tenant_id, asset_id),
    UNIQUE (tenant_id, project_id, asset_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES rai.assurance_projects (tenant_id, project_id)
);

CREATE TABLE rai.data_contracts (
    contract_id   uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id     uuid NOT NULL DEFAULT rai.current_tenant(),
    asset_id      uuid NOT NULL,
    name          text NOT NULL,
    version       integer NOT NULL CHECK (version >= 1),
    spec          jsonb NOT NULL,
    spec_sha256   char(64) NOT NULL CHECK (spec_sha256 ~ '^[0-9a-f]{64}$'),
    source        text NOT NULL,  -- the YAML or JSON exactly as published
    created_by    uuid NOT NULL REFERENCES rai.users (user_id),
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (asset_id, version),
    UNIQUE (tenant_id, contract_id),
    FOREIGN KEY (tenant_id, asset_id) REFERENCES rai.pipeline_assets (tenant_id, asset_id)
);

CREATE TABLE rai.test_cases (
    test_id         uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id       uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id      uuid NOT NULL,
    name            text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.-]{0,99}$'),
    version         integer NOT NULL CHECK (version >= 1),
    type            rai.test_type NOT NULL,
    target_asset_id uuid,
    definition      jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(definition) = 'object'),
    severity        rai.severity NOT NULL DEFAULT 'critical',
    expected_result text CHECK (length(expected_result) <= 2000),
    created_by      uuid NOT NULL REFERENCES rai.users (user_id),
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, name, version),
    UNIQUE (tenant_id, project_id, test_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES rai.assurance_projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id, target_asset_id)
        REFERENCES rai.pipeline_assets (tenant_id, project_id, asset_id)
);

CREATE TABLE rai.test_plans (
    plan_id        uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id      uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id     uuid NOT NULL,
    name           text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.-]{0,99}$'),
    version        integer NOT NULL CHECK (version >= 1),
    environment_id uuid,  -- foreign key added with environments (B-22)
    trigger        rai.plan_trigger NOT NULL DEFAULT 'manual',
    created_by     uuid NOT NULL REFERENCES rai.users (user_id),
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, name, version),
    UNIQUE (tenant_id, project_id, plan_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES rai.assurance_projects (tenant_id, project_id)
);

CREATE TABLE rai.plan_items (
    tenant_id   uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id  uuid NOT NULL,
    plan_id     uuid NOT NULL,
    position    integer NOT NULL CHECK (position >= 1),
    item_type   rai.plan_item_type NOT NULL,
    test_id     uuid,
    scenario_id uuid,  -- foreign key added with scenarios (B-26)
    contract_id uuid,
    PRIMARY KEY (plan_id, position),
    CHECK ((item_type = 'test_case' AND test_id IS NOT NULL AND scenario_id IS NULL)
        OR (item_type = 'scenario' AND scenario_id IS NOT NULL AND test_id IS NULL)),
    FOREIGN KEY (tenant_id, project_id, plan_id)
        REFERENCES rai.test_plans (tenant_id, project_id, plan_id),
    FOREIGN KEY (tenant_id, project_id, test_id)
        REFERENCES rai.test_cases (tenant_id, project_id, test_id),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES rai.data_contracts (tenant_id, contract_id)
);

CREATE FUNCTION rai.touch_project() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.row_version := OLD.row_version + 1;
    NEW.updated_at := clock_timestamp();
    NEW.project_id := OLD.project_id;
    NEW.tenant_id := OLD.tenant_id;
    NEW.created_at := OLD.created_at;
    RETURN NEW;
END
$$;
CREATE TRIGGER assurance_projects_touch BEFORE UPDATE ON rai.assurance_projects
    FOR EACH ROW EXECUTE FUNCTION rai.touch_project();

CREATE TRIGGER assurance_projects_audit AFTER INSERT OR UPDATE ON rai.assurance_projects
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('project_id', 'project');
CREATE TRIGGER pipeline_assets_audit AFTER INSERT OR UPDATE ON rai.pipeline_assets
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('asset_id', 'asset');
CREATE TRIGGER data_contracts_audit AFTER INSERT ON rai.data_contracts
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('contract_id', 'contract');
CREATE TRIGGER test_cases_audit AFTER INSERT ON rai.test_cases
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('test_id', 'test_case');
CREATE TRIGGER test_plans_audit AFTER INSERT ON rai.test_plans
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('plan_id', 'plan');
CREATE TRIGGER plan_items_audit AFTER INSERT ON rai.plan_items
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('plan_id', 'plan_item');
"""

for _table in IMMUTABLE:
    UPGRADE += f"""
CREATE TRIGGER {_table}_immutable BEFORE UPDATE OR DELETE ON rai.{_table}
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER {_table}_no_truncate BEFORE TRUNCATE ON rai.{_table}
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
"""

for _table in TABLES:
    UPGRADE += f"""
ALTER TABLE rai.{_table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.{_table} TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
"""

UPGRADE += """
GRANT SELECT, INSERT, UPDATE ON rai.assurance_projects TO rai_app;
GRANT SELECT, INSERT ON rai.pipeline_assets, rai.data_contracts, rai.test_cases,
    rai.test_plans, rai.plan_items TO rai_app;
GRANT EXECUTE ON FUNCTION rai.touch_project() TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.plan_items, rai.test_plans, rai.test_cases, rai.data_contracts,
    rai.pipeline_assets, rai.assurance_projects;
DROP FUNCTION rai.touch_project();
DROP TYPE rai.plan_item_type, rai.plan_trigger, rai.severity, rai.test_type, rai.asset_kind,
    rai.project_status, rai.data_classification;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
