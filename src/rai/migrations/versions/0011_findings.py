"""Findings: severity, owner, fix and retest (FR-10.1; prepares FR-12.2 and B-33).

A finding records a defect that a run observed. It cites the failing observation it
came from (run, step, rule; at most one finding per observation) and has a severity,
an owner and, once someone works on it, a fix reference. It ends in one of two ways:

* **closed** by a retest: a later, finished run of the same project in which the
  same rule (for the same test) passed and did not also fail. Only a passing retest
  closes a finding; the database checks it, not only the API.
* **dismissed** with a reason, by someone other than the finding's creator
  (separation of duties: it will lift a release gate once B-33 adds them).

The severity and the source never change, so a critical finding cannot be quietly
downgraded. A resolved finding never changes again, findings are never deleted, and
every change is a hash-chained audit entry (FR-01.2).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TYPE rai.finding_severity AS ENUM ('critical', 'high', 'medium', 'low');
CREATE TYPE rai.finding_status AS ENUM ('open', 'closed', 'dismissed');

CREATE TABLE rai.findings (
    finding_id      uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id       uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id      uuid NOT NULL,
    title           text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description     text CHECK (length(description) <= 4000),
    severity        rai.finding_severity NOT NULL,
    status          rai.finding_status NOT NULL DEFAULT 'open',
    source_run_id   uuid NOT NULL,
    source_step_key text NOT NULL,
    source_rule     text NOT NULL,
    owner_id        uuid NOT NULL REFERENCES rai.users (user_id),
    fix_ref         text CHECK (length(fix_ref) BETWEEN 1 AND 500),
    retest_run_id   uuid,
    resolution      text CHECK (length(resolution) BETWEEN 1 AND 2000),
    resolved_by     uuid REFERENCES rai.users (user_id),
    resolved_at     timestamptz,
    created_by      uuid NOT NULL REFERENCES rai.users (user_id),
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, finding_id),
    UNIQUE (source_run_id, source_step_key, source_rule),
    CHECK ((status = 'open') = (resolved_at IS NULL)),
    CHECK ((status = 'open') = (resolved_by IS NULL)),
    CHECK ((status = 'closed') = (retest_run_id IS NOT NULL)),
    CHECK (status <> 'dismissed' OR resolution IS NOT NULL),
    FOREIGN KEY (tenant_id, project_id) REFERENCES rai.assurance_projects (tenant_id, project_id),
    FOREIGN KEY (tenant_id, source_run_id) REFERENCES rai.runs (tenant_id, run_id),
    FOREIGN KEY (tenant_id, retest_run_id) REFERENCES rai.runs (tenant_id, run_id),
    FOREIGN KEY (source_run_id, source_step_key, source_rule)
        REFERENCES rai.observations (run_id, step_key, rule)
);
CREATE INDEX findings_project_idx ON rai.findings (project_id, status, severity);

-- Whether run `retest` passes the rule a finding's source observation failed: the
-- same rule for the same test, passed, and not also failed or errored there.
CREATE FUNCTION rai.finding_retest_passes(
    source_run uuid, source_step text, source_rule text, retest uuid
) RETURNS boolean
LANGUAGE sql STABLE AS $$
    WITH src AS (
        SELECT o.test_id FROM rai.observations o
        WHERE o.run_id = source_run AND o.step_key = source_step AND o.rule = source_rule
    )
    SELECT EXISTS (
        SELECT 1 FROM rai.observations o, src
        WHERE o.run_id = retest AND o.rule = source_rule
          AND o.test_id IS NOT DISTINCT FROM src.test_id AND o.outcome = 'pass'
    ) AND NOT EXISTS (
        SELECT 1 FROM rai.observations o, src
        WHERE o.run_id = retest AND o.rule = source_rule
          AND o.test_id IS NOT DISTINCT FROM src.test_id AND o.outcome IN ('fail', 'error')
    )
$$;

CREATE FUNCTION rai.finding_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    found_outcome text;
    found_project uuid;
    retest_project uuid;
    retest_state text;
    retest_queued timestamptz;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'open' THEN
            RAISE EXCEPTION 'a finding starts open' USING ERRCODE = 'check_violation';
        END IF;
        SELECT o.outcome::text, r.project_id INTO found_outcome, found_project
        FROM rai.observations o JOIN rai.runs r ON r.run_id = o.run_id
        WHERE o.run_id = NEW.source_run_id AND o.step_key = NEW.source_step_key
          AND o.rule = NEW.source_rule;
        IF found_project IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'the source run is not in this project' USING ERRCODE = 'check_violation';
        END IF;
        IF found_outcome NOT IN ('fail', 'error') THEN
            RAISE EXCEPTION 'a finding cites a failing or errored observation, not %', found_outcome
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status <> 'open' THEN
        RAISE EXCEPTION 'finding % is % and cannot change', OLD.finding_id, OLD.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.project_id, NEW.title, NEW.severity, NEW.source_run_id, NEW.source_step_key,
        NEW.source_rule, NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.project_id, OLD.title, OLD.severity, OLD.source_run_id, OLD.source_step_key,
        OLD.source_rule, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'a finding''s title, severity and source never change'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'closed' THEN
        SELECT project_id, state::text, queued_at INTO retest_project, retest_state, retest_queued
        FROM rai.runs WHERE run_id = NEW.retest_run_id;
        IF retest_project IS DISTINCT FROM NEW.project_id
           OR retest_state NOT IN ('passed', 'failed')
           OR retest_queued <= NEW.created_at
           OR NOT rai.finding_retest_passes(NEW.source_run_id, NEW.source_step_key,
                                            NEW.source_rule, NEW.retest_run_id) THEN
            RAISE EXCEPTION 'only a later, finished run of the project that passes the rule closes a finding'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.status = 'dismissed' AND NEW.resolved_by = NEW.created_by THEN
        RAISE EXCEPTION 'a finding is dismissed by someone other than its creator'
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$$;

CREATE TRIGGER findings_guard BEFORE INSERT OR UPDATE ON rai.findings
    FOR EACH ROW EXECUTE FUNCTION rai.finding_guard();
CREATE TRIGGER findings_no_delete BEFORE DELETE ON rai.findings
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER findings_no_truncate BEFORE TRUNCATE ON rai.findings
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER findings_audit AFTER INSERT OR UPDATE ON rai.findings
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('finding_id', 'finding');

ALTER TABLE rai.findings ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.findings TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
GRANT SELECT, INSERT, UPDATE ON rai.findings TO rai_app;
GRANT EXECUTE ON FUNCTION rai.finding_guard() TO rai_app;
GRANT EXECUTE ON FUNCTION rai.finding_retest_passes(uuid, text, text, uuid) TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.findings;
DROP FUNCTION rai.finding_guard();
DROP FUNCTION rai.finding_retest_passes(uuid, text, text, uuid);
DROP TYPE rai.finding_status;
DROP TYPE rai.finding_severity;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
