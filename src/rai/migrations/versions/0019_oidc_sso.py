"""Single sign-on through OpenID Connect (FR-01.3).

* ``user_identities`` binds an identity provider's account (``issuer``, ``subject``) to a
  RAI user. The first sign-in links them by **verified** email; afterwards only the
  binding counts, so a later change of email at the provider cannot move the account.
  SSO never creates users: a tenant admin invites them first, as before.
* ``sso_handoffs`` are one-time codes that carry a finished SSO sign-in from the API's
  callback to the web UI, which keeps its tokens in memory only. A code lives two
  minutes, works once, and the database keeps only its SHA-256.
* Both tables are reachable only through SECURITY DEFINER functions, like the password
  sign-in (migration 0002): the API role cannot read or write them directly.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-21
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE rai.user_identities (
    issuer     text NOT NULL CHECK (issuer ~ '^https?://\S+$' AND length(issuer) <= 500),
    subject    text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    user_id    uuid NOT NULL REFERENCES rai.users (user_id),
    linked_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_used_at timestamptz,
    PRIMARY KEY (issuer, subject),
    UNIQUE (issuer, user_id)  -- one account per user at each provider
);

CREATE TABLE rai.sso_handoffs (
    code_hash  char(64) PRIMARY KEY CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    user_id    uuid NOT NULL REFERENCES rai.users (user_id),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);

-- As refresh_tokens (migration 0002): RLS without a policy and no grants, so only the
-- SECURITY DEFINER functions below can read or write these tables.
ALTER TABLE rai.user_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE rai.sso_handoffs ENABLE ROW LEVEL SECURITY;

-- The user an identity signs in as, or NULL. A known (issuer, subject) is that user. An
-- unknown one links by email only when the provider says the email is verified, and only
-- to an active or invited user (an invited user becomes active: the admin invited that
-- email). A disabled user, or a user already linked to another subject at this issuer,
-- is never matched.
CREATE FUNCTION rai.oidc_sign_in(p_issuer text, p_subject text, p_email text,
                                 p_email_verified boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    found uuid;
BEGIN
    SELECT i.user_id INTO found FROM rai.user_identities i
     WHERE i.issuer = p_issuer AND i.subject = p_subject;
    IF found IS NULL THEN
        IF NOT p_email_verified OR p_email IS NULL THEN
            RETURN NULL;
        END IF;
        SELECT u.user_id INTO found FROM rai.users u
         WHERE u.email = p_email::citext AND u.status IN ('active', 'invited')
           AND NOT EXISTS (SELECT 1 FROM rai.user_identities o
                            WHERE o.issuer = p_issuer AND o.user_id = u.user_id);
        IF found IS NULL THEN
            RETURN NULL;
        END IF;
        INSERT INTO rai.user_identities (issuer, subject, user_id)
        VALUES (p_issuer, p_subject, found);
        UPDATE rai.users SET status = 'active' WHERE user_id = found AND status = 'invited';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rai.users WHERE user_id = found AND status = 'active') THEN
        RETURN NULL;
    END IF;
    UPDATE rai.user_identities SET last_used_at = clock_timestamp()
     WHERE issuer = p_issuer AND subject = p_subject;
    UPDATE rai.users SET last_login_at = clock_timestamp() WHERE user_id = found;
    RETURN found;
END
$$;

CREATE FUNCTION rai.sso_handoff_issue(p_user uuid, p_hash text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    DELETE FROM rai.sso_handoffs WHERE expires_at < clock_timestamp() - interval '1 day';
    INSERT INTO rai.sso_handoffs (code_hash, user_id, expires_at)
    VALUES (p_hash, p_user, clock_timestamp() + interval '2 minutes');
$$;

-- The user of an unused, unexpired code, which is used up; NULL otherwise.
CREATE FUNCTION rai.sso_handoff_redeem(p_hash text) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    UPDATE rai.sso_handoffs h SET used_at = clock_timestamp()
     WHERE h.code_hash = p_hash AND h.used_at IS NULL AND h.expires_at > clock_timestamp()
       AND EXISTS (SELECT 1 FROM rai.users u WHERE u.user_id = h.user_id AND u.status = 'active')
    RETURNING h.user_id
$$;

REVOKE ALL ON FUNCTION rai.oidc_sign_in(text, text, text, boolean),
    rai.sso_handoff_issue(uuid, text), rai.sso_handoff_redeem(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rai.oidc_sign_in(text, text, text, boolean),
    rai.sso_handoff_issue(uuid, text), rai.sso_handoff_redeem(text) TO rai_app;
"""

DOWNGRADE = """
DROP FUNCTION rai.sso_handoff_redeem(text);
DROP FUNCTION rai.sso_handoff_issue(uuid, text);
DROP FUNCTION rai.oidc_sign_in(text, text, text, boolean);
DROP TABLE rai.sso_handoffs, rai.user_identities;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
