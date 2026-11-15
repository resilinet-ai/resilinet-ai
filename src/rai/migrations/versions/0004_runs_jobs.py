"""Runs, steps, observations and the job queue (B-18: FR-06.1, FR-06.2, NFR-REP-1).

Data Model section 4 and Architecture section 5:

* ``runs`` pins every input (plan, test-case and contract versions with their
  SHA-256) when queued. Its state changes only along the state machine
  queued -> preflight -> running -> evaluating -> passed | failed, with side exits
  refused (preflight), aborted and error; finished runs never change (trigger).
* ``run_steps`` is the retry guard: a step's row is written in the same
  transaction as its observations, so a step is either wholly recorded or not at
  all, and never twice (primary key run_id, step_key).
* ``observations`` are insert-only, unique per (run, step, rule).
* ``jobs`` is the queue. The API (rai_app) enqueues in its tenant. The worker logs
  in as ``rai_worker``: a member of rai_app, with one extra RLS policy that lets it
  see and claim every tenant's jobs; for everything else it sets the job's tenant
  context, exactly like the API.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = r"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rai_worker') THEN
        CREATE ROLE rai_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
            NOBYPASSRLS INHERIT CONNECTION LIMIT 20;
    END IF;
END
$$;
GRANT rai_app TO rai_worker;

CREATE TYPE rai.run_state AS ENUM
    ('queued', 'preflight', 'running', 'evaluating', 'passed', 'failed', 'refused', 'aborted',
     'error');
CREATE TYPE rai.run_verdict AS ENUM ('pass', 'fail');
CREATE TYPE rai.step_state AS ENUM ('done', 'error');
CREATE TYPE rai.observation_kind AS ENUM ('rule_result', 'metric', 'refused', 'note');
CREATE TYPE rai.observation_outcome AS ENUM ('pass', 'fail', 'error', 'skipped');

CREATE TABLE rai.runs (
    run_id          uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id       uuid NOT NULL DEFAULT rai.current_tenant(),
    project_id      uuid NOT NULL,
    plan_id         uuid NOT NULL,
    environment_id  uuid,  -- foreign key added with environments (B-22)
    state           rai.run_state NOT NULL DEFAULT 'queued',
    verdict         rai.run_verdict,
    reason          text CHECK (length(reason) <= 2000),
    triggered_by    uuid NOT NULL REFERENCES rai.users (user_id),
    trigger_ref     text CHECK (length(trigger_ref) <= 200),
    idempotency_key text CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_sha256  char(64) NOT NULL,
    rai_version     text NOT NULL,
    pins            jsonb NOT NULL,
    rerun_of        uuid,
    queued_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at      timestamptz,
    finished_at     timestamptz,
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, run_id),
    FOREIGN KEY (tenant_id, project_id, plan_id)
        REFERENCES rai.test_plans (tenant_id, project_id, plan_id),
    FOREIGN KEY (tenant_id, rerun_of) REFERENCES rai.runs (tenant_id, run_id),
    CHECK ((state IN ('passed', 'failed')) = (verdict IS NOT NULL))
);
CREATE INDEX runs_project_idx ON rai.runs (tenant_id, project_id, run_id);

-- The state machine. Only state, verdict, reason and the two timestamps ever change.
CREATE FUNCTION rai.run_transition() RETURNS trigger
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
    IF (NEW.run_id, NEW.tenant_id, NEW.project_id, NEW.plan_id, NEW.environment_id,
        NEW.triggered_by, NEW.trigger_ref, NEW.idempotency_key, NEW.request_sha256,
        NEW.rai_version, NEW.pins, NEW.rerun_of, NEW.queued_at)
       IS DISTINCT FROM
       (OLD.run_id, OLD.tenant_id, OLD.project_id, OLD.plan_id, OLD.environment_id,
        OLD.triggered_by, OLD.trigger_ref, OLD.idempotency_key, OLD.request_sha256,
        OLD.rai_version, OLD.pins, OLD.rerun_of, OLD.queued_at) THEN
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
CREATE TRIGGER runs_transition BEFORE UPDATE ON rai.runs
    FOR EACH ROW EXECUTE FUNCTION rai.run_transition();
CREATE TRIGGER runs_no_delete BEFORE DELETE ON rai.runs
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER runs_audit AFTER INSERT OR UPDATE ON rai.runs
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('run_id', 'run');

CREATE TABLE rai.run_steps (
    tenant_id     uuid NOT NULL DEFAULT rai.current_tenant(),
    run_id        uuid NOT NULL,
    step_key      text NOT NULL CHECK (step_key ~ '^[a-z0-9:._-]{1,100}$'),
    plan_position integer NOT NULL CHECK (plan_position >= 1),
    state         rai.step_state NOT NULL,
    verdict       rai.run_verdict,  -- the step's own pass/fail; null when it errored
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    error         text CHECK (length(error) <= 2000),
    attempt       integer NOT NULL CHECK (attempt >= 1),
    PRIMARY KEY (run_id, step_key),
    CHECK ((state = 'done') = (verdict IS NOT NULL)),
    FOREIGN KEY (tenant_id, run_id) REFERENCES rai.runs (tenant_id, run_id)
);

CREATE TABLE rai.observations (
    observation_id uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id      uuid NOT NULL DEFAULT rai.current_tenant(),
    run_id         uuid NOT NULL,
    step_key       text NOT NULL,
    test_id        uuid,
    asset_id       uuid,
    kind           rai.observation_kind NOT NULL,
    rule           text NOT NULL CHECK (length(rule) BETWEEN 1 AND 300),
    observed_value jsonb NOT NULL DEFAULT '{}'::jsonb,
    expected       jsonb NOT NULL DEFAULT '{}'::jsonb,
    outcome        rai.observation_outcome NOT NULL,
    row_count      bigint CHECK (row_count >= 0),
    failing_count  bigint CHECK (failing_count >= 0),
    sample_ref     text,
    observed_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, step_key, rule),
    FOREIGN KEY (tenant_id, run_id) REFERENCES rai.runs (tenant_id, run_id)
);
CREATE INDEX observations_run_idx ON rai.observations (run_id, observation_id);

CREATE TABLE rai.jobs (
    job_id           uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    tenant_id        uuid NOT NULL DEFAULT rai.current_tenant(),
    run_id           uuid NOT NULL,
    kind             text NOT NULL DEFAULT 'run' CHECK (kind IN ('run')),
    available_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    locked_by        text,
    lease_expires_at timestamptz,
    last_error       text CHECK (length(last_error) <= 2000),
    finished_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES rai.runs (tenant_id, run_id)
);
CREATE INDEX jobs_claimable_idx ON rai.jobs (available_at) WHERE finished_at IS NULL;

CREATE TRIGGER run_steps_immutable BEFORE UPDATE OR DELETE ON rai.run_steps
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER observations_immutable BEFORE UPDATE OR DELETE ON rai.observations
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER run_steps_no_truncate BEFORE TRUNCATE ON rai.run_steps
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER observations_no_truncate BEFORE TRUNCATE ON rai.observations
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER runs_no_truncate BEFORE TRUNCATE ON rai.runs
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();
"""

for _table in ("runs", "run_steps", "observations", "jobs"):
    UPGRADE += f"""
ALTER TABLE rai.{_table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rai.{_table} TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
"""

UPGRADE += """
-- The worker's only exemption: it sees and claims every tenant's jobs.
CREATE POLICY worker_claims ON rai.jobs FOR SELECT TO rai_worker USING (true);
CREATE POLICY worker_leases ON rai.jobs FOR UPDATE TO rai_worker USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE ON rai.runs TO rai_app;
GRANT SELECT, INSERT ON rai.run_steps, rai.observations TO rai_app;
GRANT SELECT, INSERT ON rai.jobs TO rai_app;
GRANT SELECT, UPDATE ON rai.jobs TO rai_worker;
GRANT EXECUTE ON FUNCTION rai.run_transition() TO rai_app;
"""

DOWNGRADE = """
DROP TABLE rai.jobs, rai.observations, rai.run_steps, rai.runs;
DROP FUNCTION rai.run_transition();
DROP TYPE rai.observation_outcome, rai.observation_kind, rai.step_state, rai.run_verdict,
    rai.run_state;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'rai_worker') THEN
        REVOKE rai_app FROM rai_worker;
        DROP ROLE rai_worker;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
