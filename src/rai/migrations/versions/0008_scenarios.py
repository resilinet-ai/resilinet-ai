"""Chaos scenarios, their approvals, and scenario items in plans (B-26: FR-07.2,
NFR-SAF-1; threats T-05, R-02, E-03).

* ``chaos_scenarios`` are versioned and immutable like test cases (B-17): the app
  role has no UPDATE or DELETE grant and a trigger refuses either anyway (T-05).
  A scenario declares its blast radius (every target it may touch) and its faults,
  run in order.
* ``scenario_approvals`` is append-only (R-02). A scenario version may run only if
  its latest decision is ``approve``; a later ``reject`` withdraws it. **Separation
  of duties (E-03):** a trigger refuses a decision by the scenario's author, so the
  rule holds whatever the API does.
* ``plan_items.scenario_id`` gets its foreign key (left without one by B-17),
  including ``tenant_id`` and ``project_id``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TYPE rai.approval_decision AS ENUM ('approve', 'reject');

CREATE TABLE rai.chaos_scenarios (
    scenario_id  uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id    uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id   uuid NOT NULL,
    name         text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.-]{0,99}$'),
    version      integer NOT NULL CHECK (version >= 1),
    description  text CHECK (length(description) <= 2000),
    blast_radius text[] NOT NULL CHECK (cardinality(blast_radius) BETWEEN 1 AND 20),
    faults       jsonb NOT NULL CHECK (jsonb_typeof(faults) = 'array'
                                       AND jsonb_array_length(faults) BETWEEN 1 AND 20),
    created_by   uuid NOT NULL REFERENCES rai.users (user_id),
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (project_id, name, version),
    UNIQUE (tenant_id, scenario_id),
    UNIQUE (tenant_id, project_id, scenario_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES rai.assurance_projects (tenant_id, project_id)
);

CREATE TABLE rai.scenario_approvals (
    approval_id uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id   uuid NOT NULL DEFAULT rai.current_tenant(),
    scenario_id uuid NOT NULL,
    reviewer_id uuid NOT NULL REFERENCES rai.users (user_id),
    decision    rai.approval_decision NOT NULL,
    comment     text CHECK (length(comment) <= 2000),
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, scenario_id) REFERENCES rai.chaos_scenarios (tenant_id, scenario_id)
);
CREATE INDEX scenario_approvals_latest_idx
    ON rai.scenario_approvals (scenario_id, created_at DESC, approval_id DESC);

-- E-03: nobody reviews their own scenario. Checked in the database, not only the API.
CREATE FUNCTION rai.approval_by_another() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT FROM rai.chaos_scenarios s
                WHERE s.scenario_id = NEW.scenario_id AND s.created_by = NEW.reviewer_id) THEN
        RAISE EXCEPTION 'a scenario is reviewed by someone other than its author'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER scenario_approvals_separation BEFORE INSERT ON rai.scenario_approvals
    FOR EACH ROW EXECUTE FUNCTION rai.approval_by_another();

CREATE TRIGGER chaos_scenarios_audit AFTER INSERT ON rai.chaos_scenarios
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('scenario_id', 'scenario');
CREATE TRIGGER scenario_approvals_audit AFTER INSERT ON rai.scenario_approvals
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('approval_id', 'scenario_approval');

ALTER TABLE rai.plan_items ADD FOREIGN KEY (tenant_id, project_id, scenario_id)
    REFERENCES rai.chaos_scenarios (tenant_id, project_id, scenario_id);
"""

for _table in ("chaos_scenarios", "scenario_approvals"):
    UPGRADE += f"""
CREATE TRIGGER {_table}_immutable BEFORE UPDATE OR DELETE ON rai.{_table}
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER {_table}_no_truncate BEFORE TRUNCATE ON rai.{_table}
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
ALTER TABLE rai.{_table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.{_table} TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
GRANT SELECT, INSERT ON rai.{_table} TO rai_app;
"""

UPGRADE += """
GRANT EXECUTE ON FUNCTION rai.approval_by_another() TO rai_app;
"""

DOWNGRADE = """
ALTER TABLE rai.plan_items DROP CONSTRAINT plan_items_tenant_id_project_id_scenario_id_fkey;
DROP TABLE rai.scenario_approvals, rai.chaos_scenarios;
DROP FUNCTION rai.approval_by_another();
DROP TYPE rai.approval_decision;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
