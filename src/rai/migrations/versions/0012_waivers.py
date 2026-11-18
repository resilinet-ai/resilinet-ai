"""Waivers with expiry, and the release gate they lift (B-33: FR-12.1, FR-12.2; R-02).

The release gate of a project is blocked by every **open critical finding** (FR-10.1)
that has no waiver in force. A waiver is how a team ships knowingly with one:

* someone asks for it on an open critical finding, with a reason and an expiry at
  most 90 days ahead (``waivers``, insert-only);
* a named reviewer decides (``waiver_decisions``, append-only; the latest decision
  counts, so a later ``reject`` withdraws an approval). **No self-approval (FR-12.1,
  E-03):** a trigger refuses a decision by the waiver's requester or by the finding's
  owner, and refuses an approval once the waiver has expired or the finding is no
  longer open;
* a waiver is in force while its latest decision is ``approve``, it has not expired
  and its finding is open (``rai.finding_waiver``). When it expires the gate blocks
  again with no one having to act.

Both tables are never updated or deleted, and every row is a hash-chained audit entry.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE rai.waivers (
    waiver_id    uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id    uuid NOT NULL DEFAULT rai.current_tenant(),
    finding_id   uuid NOT NULL,
    reason       text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    expires_at   timestamptz NOT NULL,
    requested_by uuid NOT NULL REFERENCES rai.users (user_id),
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, waiver_id),
    CHECK (expires_at > created_at),
    CHECK (expires_at <= created_at + interval '90 days'),
    FOREIGN KEY (tenant_id, finding_id) REFERENCES rai.findings (tenant_id, finding_id)
);
CREATE INDEX waivers_finding_idx ON rai.waivers (finding_id, created_at);

CREATE TABLE rai.waiver_decisions (
    decision_id uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id   uuid NOT NULL DEFAULT rai.current_tenant(),
    waiver_id   uuid NOT NULL,
    reviewer_id uuid NOT NULL REFERENCES rai.users (user_id),
    decision    rai.approval_decision NOT NULL,
    comment     text CHECK (length(comment) <= 2000),
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, waiver_id) REFERENCES rai.waivers (tenant_id, waiver_id)
);
CREATE INDEX waiver_decisions_latest_idx
    ON rai.waiver_decisions (waiver_id, created_at DESC, decision_id DESC);

-- A waiver is asked for on an open critical finding: the only kind that blocks.
CREATE FUNCTION rai.waiver_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM rai.findings f WHERE f.finding_id = NEW.finding_id
                   AND f.status = 'open' AND f.severity = 'critical') THEN
        RAISE EXCEPTION 'a waiver is for an open critical finding' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- FR-12.1 / E-03: a named reviewer other than the requester and the finding's owner.
CREATE FUNCTION rai.waiver_decision_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    w rai.waivers;
    f rai.findings;
BEGIN
    SELECT * INTO w FROM rai.waivers WHERE waiver_id = NEW.waiver_id;
    SELECT * INTO f FROM rai.findings WHERE finding_id = w.finding_id;
    IF NEW.reviewer_id IN (w.requested_by, f.owner_id) THEN
        RAISE EXCEPTION 'a waiver is decided by someone other than its requester and the finding''s owner'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.decision = 'approve' AND (w.expires_at <= NEW.created_at OR f.status <> 'open') THEN
        RAISE EXCEPTION 'an expired waiver, or one on a resolved finding, cannot be approved'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- The waiver in force for a finding now, or NULL: the latest decision approves it, it
-- has not expired, and the finding is open.
CREATE FUNCTION rai.finding_waiver(finding uuid) RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT w.waiver_id
    FROM rai.waivers w JOIN rai.findings f USING (finding_id)
    WHERE w.finding_id = finding AND f.status = 'open' AND w.expires_at > now()
      AND (SELECT d.decision FROM rai.waiver_decisions d WHERE d.waiver_id = w.waiver_id
           ORDER BY d.created_at DESC, d.decision_id DESC LIMIT 1) = 'approve'
    ORDER BY w.expires_at DESC, w.waiver_id DESC
    LIMIT 1
$$;

CREATE TRIGGER waivers_guard BEFORE INSERT ON rai.waivers
    FOR EACH ROW EXECUTE FUNCTION rai.waiver_guard();
CREATE TRIGGER waiver_decisions_guard BEFORE INSERT ON rai.waiver_decisions
    FOR EACH ROW EXECUTE FUNCTION rai.waiver_decision_guard();
CREATE TRIGGER waivers_audit AFTER INSERT ON rai.waivers
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('waiver_id', 'waiver');
CREATE TRIGGER waiver_decisions_audit AFTER INSERT ON rai.waiver_decisions
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('decision_id', 'waiver_decision');
"""

for _table in ("waivers", "waiver_decisions"):
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
GRANT EXECUTE ON FUNCTION rai.waiver_guard() TO rai_app;
GRANT EXECUTE ON FUNCTION rai.waiver_decision_guard() TO rai_app;
GRANT EXECUTE ON FUNCTION rai.finding_waiver(uuid) TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.waiver_decisions, rai.waivers;
DROP FUNCTION rai.finding_waiver(uuid);
DROP FUNCTION rai.waiver_decision_guard();
DROP FUNCTION rai.waiver_guard();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
