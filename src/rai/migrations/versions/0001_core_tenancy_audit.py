"""Core tenancy and hash-chained audit (B-05: FR-01.1, FR-01.2, NFR-SEC-1, NFR-AUD-1).

Creates schema ``rai`` with tenants, users, memberships and audit_chain, plus the
database-enforced rules from Data Model section 6:

* Row-level security on every tenant table, keyed on ``app.tenant_id``. With no
  tenant set, the app role sees nothing (fails closed). The app connects as the
  non-owner role ``rai_app``, which has no BYPASSRLS.
* ``audit_chain`` is insert-only. A BEFORE INSERT trigger takes a per-tenant
  advisory lock, assigns ``seq``, stamps ``occurred_at`` and computes
  ``record_hash = SHA-256(prev_hash || canonical(row))``. ``seq`` is assigned
  after the lock, so within a tenant ``seq`` order is chain order.
* Every change to tenants and memberships writes an audit entry automatically,
  attributed to ``app.actor_type`` / ``app.actor_id`` (``system`` if unset).

The chain is tamper-evident, not tamper-proof (NFR-AUD-2): a database superuser
can rewrite it, but cannot do so without breaking verification unless they
recompute every later hash, and an exported chain head exposes that.

Revision ID: 0001
Revises:
Create Date: 2026-11-13
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE EXTENSION IF NOT EXISTS citext;
CREATE SCHEMA rai;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rai_app') THEN
        CREATE ROLE rai_app NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

-- UUIDv7: 48-bit Unix milliseconds, then random bits; version 7, RFC 9562 variant.
CREATE FUNCTION rai.uuid_v7() RETURNS uuid
LANGUAGE sql VOLATILE AS $$
    SELECT encode(
        set_bit(set_bit(
            overlay(uuid_send(gen_random_uuid())
                    PLACING substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
                    FROM 1 FOR 6),
            52, 1), 53, 1),
        'hex')::uuid
$$;

CREATE FUNCTION rai.current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE TYPE rai.tenant_status AS ENUM ('active', 'suspended');
CREATE TYPE rai.user_status AS ENUM ('invited', 'active', 'disabled');
CREATE TYPE rai.member_role AS ENUM ('admin', 'engineer', 'reviewer', 'viewer', 'auditor');
CREATE TYPE rai.actor_type AS ENUM ('user', 'api_key', 'worker', 'agent', 'system');

CREATE TABLE rai.tenants (
    tenant_id  uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    status     rai.tenant_status NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE rai.users (
    user_id         uuid PRIMARY KEY DEFAULT rai.uuid_v7(),
    email           citext NOT NULL UNIQUE CHECK (email ~ '^[^@\s]+@[^@\s]+$'),
    display_name    text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    password_hash   text,
    totp_secret_ref text,
    status          rai.user_status NOT NULL DEFAULT 'invited',
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE rai.memberships (
    tenant_id  uuid NOT NULL REFERENCES rai.tenants (tenant_id),
    user_id    uuid NOT NULL REFERENCES rai.users (user_id),
    role       rai.member_role NOT NULL,
    granted_by uuid REFERENCES rai.users (user_id),
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE SEQUENCE rai.audit_chain_seq;

CREATE TABLE rai.audit_chain (
    seq          bigint PRIMARY KEY,
    tenant_id    uuid NOT NULL REFERENCES rai.tenants (tenant_id),
    actor_type   rai.actor_type NOT NULL,
    actor_id     text NOT NULL,
    action       text NOT NULL,
    subject_type text NOT NULL,
    subject_id   text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    prev_hash    char(64) NOT NULL,
    record_hash  char(64) NOT NULL,
    occurred_at  timestamptz NOT NULL
);
CREATE INDEX audit_chain_tenant_seq_idx ON rai.audit_chain (tenant_id, seq);

-- Canonical text hashed for each entry. rai.core.audit.canonical() must match it exactly.
CREATE FUNCTION rai.audit_canonical(r rai.audit_chain) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT '['
        || to_json(r.tenant_id::text)::text || ','
        || r.seq::text || ','
        || to_json(r.actor_type::text)::text || ','
        || to_json(r.actor_id)::text || ','
        || to_json(r.action)::text || ','
        || to_json(r.subject_type)::text || ','
        || to_json(r.subject_id)::text || ','
        || to_json(to_char(r.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))::text || ','
        || r.payload::text
        || ']'
$$;

CREATE FUNCTION rai.audit_hash(prev_hash text, r rai.audit_chain) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT encode(sha256(convert_to(prev_hash || rai.audit_canonical(r), 'UTF8')), 'hex')
$$;

CREATE FUNCTION rai.audit_chain_link() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- One writer per tenant chain at a time; released at commit or rollback.
    PERFORM pg_advisory_xact_lock(7311, hashtext(NEW.tenant_id::text));
    NEW.seq := nextval('rai.audit_chain_seq');
    NEW.occurred_at := clock_timestamp();
    SELECT record_hash INTO NEW.prev_hash
      FROM rai.audit_chain WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
    NEW.prev_hash := COALESCE(NEW.prev_hash, repeat('0', 64));
    NEW.record_hash := rai.audit_hash(NEW.prev_hash, NEW);
    RETURN NEW;
END
$$;

CREATE TRIGGER audit_chain_link BEFORE INSERT ON rai.audit_chain
    FOR EACH ROW EXECUTE FUNCTION rai.audit_chain_link();

CREATE FUNCTION rai.refuse_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is insert-only (% refused)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;

CREATE TRIGGER audit_chain_insert_only BEFORE UPDATE OR DELETE ON rai.audit_chain
    FOR EACH ROW EXECUTE FUNCTION rai.refuse_change();
CREATE TRIGGER audit_chain_no_truncate BEFORE TRUNCATE ON rai.audit_chain
    FOR EACH STATEMENT EXECUTE FUNCTION rai.refuse_change();

-- Recompute a tenant's chain in the database; returns the first broken seq, if any.
CREATE FUNCTION rai.audit_verify(p_tenant uuid)
RETURNS TABLE (checked bigint, first_break_seq bigint, reason text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r rai.audit_chain;
    expected_prev text := repeat('0', 64);
    n bigint := 0;
BEGIN
    FOR r IN SELECT * FROM rai.audit_chain WHERE tenant_id = p_tenant ORDER BY seq LOOP
        n := n + 1;
        IF r.prev_hash <> expected_prev THEN
            RETURN QUERY SELECT n, r.seq, 'prev_hash does not match the previous record'::text;
            RETURN;
        END IF;
        IF r.record_hash <> rai.audit_hash(r.prev_hash, r) THEN
            RETURN QUERY SELECT n, r.seq, 'record_hash does not match the record content'::text;
            RETURN;
        END IF;
        expected_prev := r.record_hash;
    END LOOP;
    RETURN QUERY SELECT n, NULL::bigint, NULL::text;
END
$$;

-- Automatic audit entries for tenant and membership changes.
-- TG_ARGV[0]: column holding the subject id; TG_ARGV[1]: subject_type.
CREATE FUNCTION rai.audit_row_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_row jsonb := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END;
    new_row jsonb := CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) END;
    current_row jsonb := COALESCE(new_row, old_row);
BEGIN
    INSERT INTO rai.audit_chain (tenant_id, actor_type, actor_id, action, subject_type, subject_id, payload)
    VALUES (
        (current_row ->> 'tenant_id')::uuid,
        COALESCE(NULLIF(current_setting('app.actor_type', true), ''), 'system')::rai.actor_type,
        COALESCE(NULLIF(current_setting('app.actor_id', true), ''), 'unknown'),
        TG_ARGV[1] || '.' || lower(TG_OP),
        TG_ARGV[1],
        current_row ->> TG_ARGV[0],
        jsonb_strip_nulls(jsonb_build_object('old', old_row, 'new', new_row))
    );
    RETURN NULL;
END
$$;

CREATE TRIGGER tenants_audit AFTER INSERT OR UPDATE ON rai.tenants
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('tenant_id', 'tenant');
CREATE TRIGGER memberships_audit AFTER INSERT OR UPDATE OR DELETE ON rai.memberships
    FOR EACH ROW EXECUTE FUNCTION rai.audit_row_change('user_id', 'membership');

-- Row-level security. The owner (migrations, bootstrap) is exempt; rai_app is not.
ALTER TABLE rai.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE rai.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE rai.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE rai.audit_chain ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON rai.tenants TO rai_app
    USING (tenant_id = rai.current_tenant());
CREATE POLICY tenant_isolation ON rai.memberships TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
CREATE POLICY tenant_isolation ON rai.audit_chain TO rai_app
    USING (tenant_id = rai.current_tenant()) WITH CHECK (tenant_id = rai.current_tenant());
-- Users are global (one person, several tenants): visible only through a membership
-- in the current tenant. Creating a user (invite) is allowed; reading it back needs
-- the membership.
CREATE POLICY members_of_current_tenant ON rai.users FOR SELECT TO rai_app
    USING (EXISTS (SELECT 1 FROM rai.memberships m
                   WHERE m.user_id = users.user_id AND m.tenant_id = rai.current_tenant()));
CREATE POLICY members_of_current_tenant_update ON rai.users FOR UPDATE TO rai_app
    USING (EXISTS (SELECT 1 FROM rai.memberships m
                   WHERE m.user_id = users.user_id AND m.tenant_id = rai.current_tenant()));
CREATE POLICY invite ON rai.users FOR INSERT TO rai_app WITH CHECK (true);

GRANT USAGE ON SCHEMA rai TO rai_app;
GRANT SELECT, UPDATE ON rai.tenants TO rai_app;
GRANT SELECT, INSERT, UPDATE ON rai.users TO rai_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON rai.memberships TO rai_app;
GRANT SELECT, INSERT ON rai.audit_chain TO rai_app;
GRANT USAGE ON SEQUENCE rai.audit_chain_seq TO rai_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rai TO rai_app;
"""

DOWNGRADE = r"""
DROP SCHEMA rai CASCADE;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'rai_app') THEN
        REVOKE ALL ON SCHEMA public FROM rai_app;
        DROP ROLE rai_app;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
