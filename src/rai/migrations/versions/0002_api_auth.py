"""API login role, refresh tokens and the narrow pre-tenant functions (B-16: FR-01.1, NFR-SEC-1).

* ``rai_api`` is the role the API logs in as: LOGIN, no superuser, no BYPASSRLS,
  not the schema owner, and a member of ``rai_app`` only (threat I-02). A code path
  that forgets ``set_context`` therefore sees zero rows, never every tenant. Its
  password is set by ``rai db set-api-password``, never in a migration.
* ``refresh_tokens`` stores only SHA-256 hashes. ``rai_app`` has no grants on it;
  it is reached only through the functions below.
* A few steps must happen before a tenant is chosen (login, "my tenants", token
  rotation) or must not reveal other tenants (invite, threat I-03). They are
  SECURITY DEFINER functions with a fixed search_path, executable by ``rai_app``
  only, each doing one narrow thing.
* A tenant always keeps at least one admin (checked under a per-tenant lock, so two
  admins cannot demote each other at the same time).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = r"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rai_api') THEN
        CREATE ROLE rai_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
            NOBYPASSRLS INHERIT CONNECTION LIMIT 50;
    END IF;
END
$$;
GRANT rai_app TO rai_api;

CREATE TABLE rai.refresh_tokens (
    token_hash char(64) PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES rai.users (user_id),
    family_id  uuid NOT NULL,
    issued_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz,
    revoked_at timestamptz
);
CREATE INDEX refresh_tokens_family_idx ON rai.refresh_tokens (family_id);
CREATE INDEX refresh_tokens_user_idx ON rai.refresh_tokens (user_id);
ALTER TABLE rai.refresh_tokens ENABLE ROW LEVEL SECURITY;  -- no policy: no direct access

-- Login: the stored hash for one email (the API verifies it with Argon2id).
CREATE FUNCTION rai.auth_lookup(p_email text)
RETURNS TABLE (user_id uuid, password_hash text, status rai.user_status)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.user_id, u.password_hash, u.status FROM rai.users u WHERE u.email = p_email::citext
$$;

CREATE FUNCTION rai.auth_record_login(p_user uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    UPDATE rai.users SET last_login_at = clock_timestamp() WHERE user_id = p_user
$$;

-- The signed-in user's own profile and active memberships (before a tenant is chosen).
CREATE FUNCTION rai.user_profile(p_user uuid)
RETURNS TABLE (user_id uuid, email text, display_name text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.user_id, u.email::text, u.display_name
      FROM rai.users u WHERE u.user_id = p_user AND u.status = 'active'
$$;

CREATE FUNCTION rai.user_tenants(p_user uuid)
RETURNS TABLE (tenant_id uuid, slug text, name text, role rai.member_role)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT t.tenant_id, t.slug, t.name, m.role
      FROM rai.memberships m
      JOIN rai.tenants t ON t.tenant_id = m.tenant_id
      JOIN rai.users u ON u.user_id = m.user_id
     WHERE m.user_id = p_user AND t.status = 'active' AND u.status = 'active'
     ORDER BY t.slug
$$;

-- Refresh tokens: issue at login; rotate on every use. A family shares one absolute
-- expiry, so a session ends at the latest p_ttl after login. Presenting a token that
-- was already used revokes its whole family (it was probably stolen).
CREATE FUNCTION rai.refresh_issue(p_user uuid, p_hash text, p_ttl interval) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    INSERT INTO rai.refresh_tokens (token_hash, user_id, family_id, expires_at)
    VALUES (p_hash, p_user, rai.uuid_v7(), clock_timestamp() + p_ttl)
$$;

CREATE FUNCTION rai.refresh_rotate(p_old text, p_new text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    t rai.refresh_tokens;
BEGIN
    SELECT * INTO t FROM rai.refresh_tokens WHERE token_hash = p_old FOR UPDATE;
    IF NOT FOUND OR t.revoked_at IS NOT NULL OR t.expires_at <= clock_timestamp() THEN
        RETURN NULL;
    END IF;
    IF t.used_at IS NOT NULL THEN
        UPDATE rai.refresh_tokens SET revoked_at = clock_timestamp()
         WHERE family_id = t.family_id AND revoked_at IS NULL;
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rai.users WHERE user_id = t.user_id AND status = 'active') THEN
        RETURN NULL;
    END IF;
    UPDATE rai.refresh_tokens SET used_at = clock_timestamp() WHERE token_hash = p_old;
    INSERT INTO rai.refresh_tokens (token_hash, user_id, family_id, expires_at)
    VALUES (p_new, t.user_id, t.family_id, t.expires_at);
    RETURN t.user_id;
END
$$;

CREATE FUNCTION rai.refresh_revoke(p_user uuid, p_hash text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    UPDATE rai.refresh_tokens SET revoked_at = clock_timestamp()
     WHERE revoked_at IS NULL AND family_id IN (
         SELECT family_id FROM rai.refresh_tokens WHERE token_hash = p_hash AND user_id = p_user)
$$;

-- Invite (threat I-03): link an existing account or create an invited one, then add
-- the membership. Same outcome whether the email is new, belongs to someone in
-- another tenant, or is already a member here: nothing is returned, nothing leaks.
-- Defence in depth: the caller must be an admin of the current tenant.
CREATE FUNCTION rai.invite_member(p_email text, p_display_name text, p_role rai.member_role)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_tenant uuid := rai.current_tenant();
    v_actor  text := current_setting('app.actor_id', true);
    v_user   uuid;
BEGIN
    IF v_tenant IS NULL OR current_setting('app.actor_type', true) IS DISTINCT FROM 'user'
       OR NOT EXISTS (SELECT 1 FROM rai.memberships m
                       WHERE m.tenant_id = v_tenant AND m.user_id::text = v_actor
                         AND m.role = 'admin') THEN
        RAISE EXCEPTION 'only an admin of the current tenant can invite'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO rai.users (email, display_name) VALUES (p_email, p_display_name)
        ON CONFLICT (email) DO NOTHING;
    SELECT u.user_id INTO v_user FROM rai.users u WHERE u.email = p_email::citext;
    INSERT INTO rai.memberships (tenant_id, user_id, role, granted_by)
    VALUES (v_tenant, v_user, p_role, v_actor::uuid)
        ON CONFLICT (tenant_id, user_id) DO NOTHING;
END
$$;

REVOKE ALL ON FUNCTION rai.auth_lookup(text), rai.auth_record_login(uuid),
    rai.user_profile(uuid), rai.user_tenants(uuid), rai.refresh_issue(uuid, text, interval),
    rai.refresh_rotate(text, text), rai.refresh_revoke(uuid, text),
    rai.invite_member(text, text, rai.member_role) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rai.auth_lookup(text), rai.auth_record_login(uuid),
    rai.user_profile(uuid), rai.user_tenants(uuid), rai.refresh_issue(uuid, text, interval),
    rai.refresh_rotate(text, text), rai.refresh_revoke(uuid, text),
    rai.invite_member(text, text, rai.member_role) TO rai_app;

-- Every tenant keeps at least one admin.
CREATE FUNCTION rai.keep_an_admin() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.role <> 'admin' OR (TG_OP = 'UPDATE' AND NEW.role = 'admin') THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    PERFORM pg_advisory_xact_lock(7312, hashtext(OLD.tenant_id::text));
    IF NOT EXISTS (SELECT 1 FROM rai.memberships m
                    WHERE m.tenant_id = OLD.tenant_id AND m.role = 'admin'
                      AND m.user_id <> OLD.user_id) THEN
        RAISE EXCEPTION 'a tenant must keep at least one admin'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END
$$;
CREATE TRIGGER memberships_keep_an_admin BEFORE UPDATE OR DELETE ON rai.memberships
    FOR EACH ROW EXECUTE FUNCTION rai.keep_an_admin();
"""

DOWNGRADE = r"""
DROP TRIGGER memberships_keep_an_admin ON rai.memberships;
DROP FUNCTION rai.keep_an_admin();
DROP FUNCTION rai.invite_member(text, text, rai.member_role);
DROP FUNCTION rai.refresh_revoke(uuid, text);
DROP FUNCTION rai.refresh_rotate(text, text);
DROP FUNCTION rai.refresh_issue(uuid, text, interval);
DROP FUNCTION rai.user_tenants(uuid);
DROP FUNCTION rai.user_profile(uuid);
DROP FUNCTION rai.auth_record_login(uuid);
DROP FUNCTION rai.auth_lookup(text);
DROP TABLE rai.refresh_tokens;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'rai_api') THEN
        REVOKE rai_app FROM rai_api;
        DROP ROLE rai_api;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
