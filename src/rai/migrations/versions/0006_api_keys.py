"""Project-scoped API keys for CI (B-20: FR-06.1; threats S-02, D-03).

* ``api_keys`` stores only the SHA-256 of each key's secret, with a prefix that
  identifies it. ``expires_at`` is required and at most 90 days after creation.
  The only change a key ever takes is its revocation, once (trigger); keys are
  never deleted. Inserts and the revocation are audited.
* ``api_key_lookup`` is the one pre-tenant path to a key (SECURITY DEFINER, like
  ``auth_lookup``). It returns a key only while it is unrevoked and unexpired,
  its tenant and project are active, and the user who created it is still an
  active admin or engineer of the tenant: a key never outlives its creator's access.
* ``runs.api_key_id`` records which key started a run (``triggered_by`` stays the
  accountable user, the key's creator); ``run_transition`` freezes it with the
  other inputs.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _run_transition(frozen: str) -> str:
    """The B-18 state machine; ``frozen`` lists the columns that never change."""
    old = ", ".join(f"OLD.{c}" for c in frozen.split(", "))
    new = ", ".join(f"NEW.{c}" for c in frozen.split(", "))
    return f"""
CREATE OR REPLACE FUNCTION rai.run_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed text[] := CASE OLD.state
        WHEN 'queued'     THEN ARRAY['preflight', 'aborted', 'error']
        WHEN 'preflight'  THEN ARRAY['running', 'refused', 'aborted', 'error']
        WHEN 'running'    THEN ARRAY['evaluating', 'aborted', 'error']
        WHEN 'evaluating' THEN ARRAY['passed', 'failed', 'aborted', 'error']
        ELSE ARRAY[]::text[]
    END;
BEGIN
    IF OLD.state IN ('passed', 'failed', 'refused', 'aborted', 'error') THEN
        RAISE EXCEPTION 'run % is finished (%); start a new run instead', OLD.run_id, OLD.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT (NEW.state::text = ANY (allowed)) THEN
        RAISE EXCEPTION 'run state cannot go from % to %', OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF ({new}) IS DISTINCT FROM ({old}) THEN
        RAISE EXCEPTION 'only the state of a run changes; its inputs are pinned'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state = 'preflight' AND NEW.started_at IS NULL THEN
        NEW.started_at := clock_timestamp();
    END IF;
    IF NEW.state IN ('passed', 'failed', 'refused', 'aborted', 'error') THEN
        NEW.finished_at := clock_timestamp();
    END IF;
    RETURN NEW;
END
$$;
"""


FROZEN_B18 = (
    "run_id, tenant_id, project_id, plan_id, environment_id, triggered_by, trigger_ref, "
    "idempotency_key, request_sha256, rai_version, pins, rerun_of, queued_at"
)

UPGRADE = r"""
CREATE TYPE rai.api_key_scope AS ENUM ('runs:start', 'runs:read');

CREATE TABLE rai.api_keys (
    api_key_id uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id  uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id uuid NOT NULL,
    name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    key_prefix text NOT NULL UNIQUE CHECK (key_prefix ~ '^[a-z2-7]{12}$'),
    key_hash   char(64) NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    scopes     rai.api_key_scope[] NOT NULL CHECK (cardinality(scopes) >= 1),
    created_by uuid NOT NULL REFERENCES rai.users (user_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by uuid REFERENCES rai.users (user_id),
    UNIQUE (tenant_id, api_key_id),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '90 days'),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    FOREIGN KEY (tenant_id, project_id) REFERENCES rai.assurance_projects (tenant_id, project_id)
);
CREATE INDEX api_keys_project_idx ON rai.api_keys (tenant_id, project_id, api_key_id);

-- A key changes once: it is revoked. Nothing else about it ever changes.
CREATE FUNCTION rai.api_key_revoke_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
       OR (NEW.api_key_id, NEW.tenant_id, NEW.project_id, NEW.name, NEW.key_prefix,
           NEW.key_hash, NEW.scopes, NEW.created_by, NEW.created_at, NEW.expires_at)
          IS DISTINCT FROM
          (OLD.api_key_id, OLD.tenant_id, OLD.project_id, OLD.name, OLD.key_prefix,
           OLD.key_hash, OLD.scopes, OLD.created_by, OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'an API key can only be revoked, once; create a new key instead'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER api_keys_revoke_only BEFORE UPDATE ON rai.api_keys
    FOR EACH ROW EXECUTE FUNCTION rai.api_key_revoke_only();
CREATE TRIGGER api_keys_no_delete BEFORE DELETE ON rai.api_keys
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER api_keys_no_truncate BEFORE TRUNCATE ON rai.api_keys
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER api_keys_audit AFTER INSERT OR UPDATE ON rai.api_keys
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('api_key_id', 'api_key');

ALTER TABLE rai.api_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.api_keys TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());

-- The one pre-tenant path to a key: usable keys only.
CREATE FUNCTION rai.api_key_lookup(p_prefix text)
RETURNS TABLE (api_key_id uuid, tenant_id uuid, project_id uuid, key_hash text,
               scopes text[], created_by uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT k.api_key_id, k.tenant_id, k.project_id, k.key_hash::text, k.scopes::text[],
           k.created_by
      FROM rai.api_keys k
      JOIN rai.tenants t ON t.tenant_id = k.tenant_id AND t.status = 'active'
      JOIN rai.assurance_projects p
        ON p.tenant_id = k.tenant_id AND p.project_id = k.project_id AND p.status = 'active'
      JOIN rai.users u ON u.user_id = k.created_by AND u.status = 'active'
      JOIN rai.memberships m
        ON m.tenant_id = k.tenant_id AND m.user_id = k.created_by
       AND m.role IN ('admin', 'engineer')
     WHERE k.key_prefix = p_prefix AND k.revoked_at IS NULL AND k.expires_at > clock_timestamp()
$$;
REVOKE ALL ON FUNCTION rai.api_key_lookup(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rai.api_key_lookup(text) TO rai_app;

GRANT SELECT, INSERT, UPDATE ON rai.api_keys TO rai_app;
GRANT EXECUTE ON FUNCTION rai.api_key_revoke_only() TO rai_app;

ALTER TABLE rai.runs ADD COLUMN api_key_id uuid;
ALTER TABLE rai.runs ADD FOREIGN KEY (tenant_id, api_key_id)
    REFERENCES rai.api_keys (tenant_id, api_key_id);
"""
UPGRADE += _run_transition(FROZEN_B18 + ", api_key_id")

DOWNGRADE = (
    _run_transition(FROZEN_B18)
    + """
ALTER TABLE rai.runs DROP COLUMN api_key_id;
DROP FUNCTION rai.api_key_lookup(text);
DROP TABLE rai.api_keys;
DROP FUNCTION rai.api_key_revoke_only();
DROP TYPE rai.api_key_scope;
"""
)


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
