"""Release decisions: a named reviewer approves or rejects a release (B-42: FR-12.1, FR-12.2).

The release gate (migration 0012) says whether a project *may* ship. A release decision
records that a named person decided it *should*, for one labelled release (``v1.4``):

* ``release_decisions`` is append-only. For a project and label, the latest decision
  counts, so a later ``reject`` withdraws an approval and a later ``approve`` replaces
  a rejection. Every row is a hash-chained audit entry.
* **The gate's state is recorded by the database, not the caller.** A trigger computes,
  at the moment of the decision, how many open critical findings block the gate and
  which findings are waived under which waivers, and overwrites whatever was sent.
* **No approval while the gate is blocked (FR-12.2).** The same trigger refuses it.
* **No approving your own exception (FR-12.1, E-03).** An approval that relies on a
  waiver is refused from the waiver's requester or the waived finding's owner.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-20
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE rai.release_decisions (
    decision_id     uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id       uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id      uuid NOT NULL,
    release_label   text NOT NULL CHECK (release_label ~ '^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$'),
    decision        rai.approval_decision NOT NULL,
    justification   text NOT NULL CHECK (length(justification) BETWEEN 1 AND 2000),
    reviewer_id     uuid NOT NULL REFERENCES rai.users (user_id),
    -- The gate at the moment of the decision, set by the trigger below.
    gate_verdict    text NOT NULL CHECK (gate_verdict IN ('pass', 'blocked')),
    blocking_findings uuid[] NOT NULL,
    waived_findings uuid[] NOT NULL,
    waivers         uuid[] NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (decision = 'reject' OR gate_verdict = 'pass'),
    FOREIGN KEY (tenant_id, project_id) REFERENCES rai.assurance_projects (tenant_id, project_id)
);
CREATE INDEX release_decisions_latest_idx
    ON rai.release_decisions (project_id, release_label, created_at DESC, decision_id DESC);

CREATE FUNCTION rai.release_decision_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    conflicted int;
BEGIN
    -- The gate now, as GET /v1/projects/{id}/gate computes it.
    SELECT coalesce(array_agg(f.finding_id ORDER BY f.finding_id)
                        FILTER (WHERE rai.finding_waiver(f.finding_id) IS NULL), '{}'),
           coalesce(array_agg(f.finding_id ORDER BY f.finding_id)
                        FILTER (WHERE rai.finding_waiver(f.finding_id) IS NOT NULL), '{}'),
           coalesce(array_agg(rai.finding_waiver(f.finding_id) ORDER BY f.finding_id)
                        FILTER (WHERE rai.finding_waiver(f.finding_id) IS NOT NULL), '{}')
      INTO NEW.blocking_findings, NEW.waived_findings, NEW.waivers
      FROM rai.findings f
     WHERE f.project_id = NEW.project_id AND f.status = 'open' AND f.severity = 'critical';
    NEW.gate_verdict := CASE WHEN cardinality(NEW.blocking_findings) = 0 THEN 'pass' ELSE 'blocked' END;
    NEW.created_at := clock_timestamp();

    IF NEW.decision = 'approve' AND NEW.gate_verdict = 'blocked' THEN
        RAISE EXCEPTION 'a release cannot be approved while its gate is blocked'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.decision = 'approve' THEN
        SELECT count(*) INTO conflicted
          FROM rai.waivers w JOIN rai.findings f USING (finding_id)
         WHERE w.waiver_id = ANY (NEW.waivers)
           AND NEW.reviewer_id IN (w.requested_by, f.owner_id);
        IF conflicted > 0 THEN
            RAISE EXCEPTION 'a release that relies on your own waiver or finding is approved by someone else'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER release_decisions_guard BEFORE INSERT ON rai.release_decisions
    FOR EACH ROW EXECUTE FUNCTION rai.release_decision_guard();
CREATE TRIGGER release_decisions_audit AFTER INSERT ON rai.release_decisions
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('decision_id', 'release_decision');
CREATE TRIGGER release_decisions_immutable BEFORE UPDATE OR DELETE ON rai.release_decisions
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER release_decisions_no_truncate BEFORE TRUNCATE ON rai.release_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
ALTER TABLE rai.release_decisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.release_decisions TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
GRANT SELECT, INSERT ON rai.release_decisions TO rai_app;
GRANT EXECUTE ON FUNCTION rai.release_decision_guard() TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.release_decisions;
DROP FUNCTION rai.release_decision_guard();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
