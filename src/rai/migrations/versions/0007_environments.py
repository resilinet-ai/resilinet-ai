"""Environments and the target allow-list (B-22: NFR-SAF-1, FR-07.2).

* ``environments`` registers where RAI may act. **Production cannot be
  represented**: the ``env_type`` enum has only SANDBOX and STAGING, a check holds
  ``production_flag`` false, and names may not contain "PROD" (defence in depth,
  so a production system cannot be registered under a sandbox type by mistake).
  Name, type and the flag never change (trigger); environments are retired, never
  deleted. ``max_fault_duration`` (default 15 min, at most 60) is the ceiling every
  scenario in the environment must respect.
* ``environment_targets`` is the allow-list of injectable and probeable targets.
  Container and proxy targets must name the label the target has to carry
  (``required_label``), which the chaos agent checks before acting (B-24). Targets
  are added or removed, never edited; a retired environment takes no new ones.
* The ``environment_id`` columns left by B-17 and B-18 (projects' default,
  plans, runs) get their foreign keys, including ``tenant_id``.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-14
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TYPE rai.env_type AS ENUM ('SANDBOX', 'STAGING');  -- no PRODUCTION value, by design
CREATE TYPE rai.env_status AS ENUM ('active', 'retired');
CREATE TYPE rai.target_kind AS ENUM ('container', 'proxy', 'db', 'http');

CREATE TABLE rai.environments (
    environment_id     uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id          uuid NOT NULL DEFAULT rai.current_tenant() REFERENCES rai.tenants (tenant_id),
    name               text NOT NULL CHECK (name ~ '^[A-Z][A-Z0-9-]{1,62}$' AND name !~ 'PROD'),
    type               rai.env_type NOT NULL,
    production_flag    boolean NOT NULL DEFAULT false CHECK (production_flag = false),
    max_fault_duration interval NOT NULL DEFAULT interval '15 minutes'
                       CHECK (max_fault_duration > interval '0'
                              AND max_fault_duration <= interval '60 minutes'),
    status             rai.env_status NOT NULL DEFAULT 'active',
    row_version        integer NOT NULL DEFAULT 1,
    created_by         uuid NOT NULL REFERENCES rai.users (user_id),
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, name),
    UNIQUE (tenant_id, environment_id)
);

-- Only the fault ceiling and the status change; everything that identifies an
-- environment (and its safety class) is fixed at registration.
CREATE FUNCTION rai.environment_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.environment_id, NEW.tenant_id, NEW.name, NEW.type, NEW.production_flag,
        NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.environment_id, OLD.tenant_id, OLD.name, OLD.type, OLD.production_flag,
        OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'an environment''s name and type never change; register a new one'
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.row_version := OLD.row_version + 1;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$$;
CREATE TRIGGER environments_change BEFORE UPDATE ON rai.environments
    FOR EACH ROW EXECUTE FUNCTION rai.environment_change();
CREATE TRIGGER environments_no_delete BEFORE DELETE ON rai.environments
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER environments_audit AFTER INSERT OR UPDATE ON rai.environments
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('environment_id', 'environment');

CREATE TABLE rai.environment_targets (
    target_id      uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id      uuid NOT NULL DEFAULT rai.current_tenant(),
    environment_id uuid NOT NULL,
    kind           rai.target_kind NOT NULL,
    name           text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_.-]{0,62}$'),
    required_label text CHECK (required_label ~ '^[a-z0-9][a-z0-9_./-]{0,99}=[A-Za-z0-9_.-]{1,100}$'),
    health_url     text CHECK (health_url ~ '^https?://\S+$' AND length(health_url) <= 500),
    created_by     uuid NOT NULL REFERENCES rai.users (user_id),
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (environment_id, name),
    CHECK (kind NOT IN ('container', 'proxy') OR required_label IS NOT NULL),
    FOREIGN KEY (tenant_id, environment_id) REFERENCES rai.environments (tenant_id, environment_id)
);

CREATE FUNCTION rai.target_needs_active_environment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM rai.environments e
                    WHERE e.environment_id = NEW.environment_id AND e.status = 'active') THEN
        RAISE EXCEPTION 'environment % is retired; it takes no new targets', NEW.environment_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER environment_targets_active BEFORE INSERT ON rai.environment_targets
    FOR EACH ROW EXECUTE FUNCTION rai.target_needs_active_environment();
CREATE TRIGGER environment_targets_no_update BEFORE UPDATE ON rai.environment_targets
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER environment_targets_audit AFTER INSERT OR DELETE ON rai.environment_targets
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('target_id', 'environment_target');

ALTER TABLE rai.assurance_projects ADD FOREIGN KEY (tenant_id, default_environment_id)
    REFERENCES rai.environments (tenant_id, environment_id);
ALTER TABLE rai.test_plans ADD FOREIGN KEY (tenant_id, environment_id)
    REFERENCES rai.environments (tenant_id, environment_id);
ALTER TABLE rai.runs ADD FOREIGN KEY (tenant_id, environment_id)
    REFERENCES rai.environments (tenant_id, environment_id);
"""

for _table in ("environments", "environment_targets"):
    UPGRADE += f"""
ALTER TABLE rai.{_table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.{_table} TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
"""

UPGRADE += """
GRANT SELECT, INSERT, UPDATE ON rai.environments TO rai_app;
GRANT SELECT, INSERT, DELETE ON rai.environment_targets TO rai_app;
GRANT EXECUTE ON FUNCTION rai.environment_change(), rai.target_needs_active_environment()
    TO rai_app;
"""

DOWNGRADE = """
ALTER TABLE rai.runs DROP CONSTRAINT runs_tenant_id_environment_id_fkey;
ALTER TABLE rai.test_plans DROP CONSTRAINT test_plans_tenant_id_environment_id_fkey;
ALTER TABLE rai.assurance_projects
    DROP CONSTRAINT assurance_projects_tenant_id_default_environment_id_fkey;
DROP TABLE rai.environment_targets, rai.environments;
DROP FUNCTION rai.target_needs_active_environment(), rai.environment_change();
DROP TYPE rai.target_kind, rai.env_status, rai.env_type;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
