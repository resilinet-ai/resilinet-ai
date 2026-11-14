"""The API against a real PostgreSQL 16 (B-16: FR-01.1, NFR-SEC-1).

Acceptance (spec FR-01.1): a user in tenant A cannot read any tenant B object.
Threat-model criteria: the API logs in as rai_api only (I-02); invites answer the
same whatever the email (I-03); another tenant's objects are 404 (I-07); uniform
login errors and throttling (S-01); the audit actor comes from the token (S-04).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from typer.testing import CliRunner

from rai.api.app import create_app
from rai.api.context import check_login_role
from rai.api.settings import Settings, SettingsError
from rai.api.tokens import issue_access_token
from rai.cli.main import app as cli
from rai.core import db
from rai.core.passwords import hash_password
from rai.core.tenancy import set_context
from tests.integration.apikit import (
    API_PASSWORD,
    DSN,
    PASSWORD,
    SECRET,
    activate,
    auth,
    email,
    login,
    member,
    needs_db,
    new_tenant,
)

pytestmark = needs_db


# ---- I-02: the API's database role ------------------------------------------------------


def test_api_refuses_to_start_as_the_schema_owner(owner: Engine) -> None:
    settings = Settings(database_url=db.sqlalchemy_url(DSN), jwt_secret=SECRET)
    with (
        pytest.raises(SettingsError, match="must log in as rai_api"),
        TestClient(create_app(settings)),
    ):
        pass


def test_api_refuses_to_start_on_an_unmigrated_database(owner: Engine) -> None:
    name = f"rai_empty_{uuid4().hex[:8]}"
    with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    empty = create_engine(make_url(db.sqlalchemy_url(DSN)).set(database=name))
    try:
        with empty.connect() as conn, pytest.raises(SettingsError, match="rai db upgrade"):
            check_login_role(conn)
    finally:
        empty.dispose()
        with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'DROP DATABASE "{name}"'))


def test_rai_api_without_tenant_context_sees_nothing(owner: Engine, api_url: str) -> None:
    new_tenant(owner)
    engine = create_engine(api_url)
    try:
        with engine.begin() as conn:
            for table in ("tenants", "memberships", "users", "audit_chain"):
                count = conn.execute(text(f"SELECT count(*) FROM rai.{table}")).scalar_one()
                assert count == 0, table
        with pytest.raises(ProgrammingError, match="permission denied"), engine.begin() as conn:
            conn.execute(text("SELECT count(*) FROM rai.refresh_tokens"))
        with pytest.raises(ProgrammingError), engine.begin() as conn:
            conn.execute(text("INSERT INTO rai.tenants (name, slug) VALUES ('x', 'x-no')"))
    finally:
        engine.dispose()


# ---- sign-in ------------------------------------------------------------------------------


def test_login_and_me(client: TestClient, owner: Engine) -> None:
    tenant = new_tenant(owner)
    tokens = login(client, tenant.admin_email.upper())  # emails are case-insensitive
    assert (tokens["token_type"], tokens["expires_in"]) == ("bearer", 900)
    me = client.get("/v1/me", headers=auth(tokens))
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == tenant.admin_email
    assert [(t["tenant_id"], t["role"]) for t in body["tenants"]] == [
        (str(tenant.tenant_id), "admin")
    ]
    assert client.get("/v1/tenants", headers=auth(tokens)).json() == body["tenants"]
    assert me.headers["cache-control"] == "no-store"


def test_login_failures_look_the_same(client: TestClient, owner: Engine) -> None:
    tenant = new_tenant(owner)
    invited = member(client, tenant, "viewer")  # invited: no password yet
    attempts = [
        (tenant.admin_email, "wrong password!"),
        (email(), PASSWORD),
        (invited, PASSWORD),
    ]
    answers = [client.post("/v1/auth/login", json={"email": e, "password": p}) for e, p in attempts]
    assert {a.status_code for a in answers} == {401}
    assert len({a.text for a in answers}) == 1
    assert answers[0].headers["content-type"] == "application/problem+json"
    assert answers[0].json()["code"] == "auth.invalid_credentials"


def test_repeated_failures_are_throttled_even_for_unknown_emails(client: TestClient) -> None:
    address = email()
    for _ in range(3):
        failed = client.post("/v1/auth/login", json={"email": address, "password": "nope"})
        assert failed.status_code == 401
    blocked = client.post("/v1/auth/login", json={"email": address, "password": "nope"})
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["code"] == "auth.too_many_attempts"


def test_refresh_rotates_and_reuse_ends_the_session(client: TestClient, owner: Engine) -> None:
    tenant = new_tenant(owner)
    first = login(client, tenant.admin_email)
    second = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    rotated = second.json()
    assert rotated["refresh_token"] != first["refresh_token"]
    assert client.get("/v1/me", headers=auth(rotated)).status_code == 200
    replay = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401  # the old token was already used
    stolen = client.post("/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert stolen.status_code == 401  # ...so its whole family is revoked


def test_logout_revokes_the_refresh_token(client: TestClient, owner: Engine) -> None:
    tenant = new_tenant(owner)
    tokens = login(client, tenant.admin_email)
    body = {"refresh_token": tokens["refresh_token"]}
    assert client.post("/v1/auth/logout", json=body).status_code == 401  # needs sign-in
    assert client.post("/v1/auth/logout", json=body, headers=auth(tokens)).status_code == 204
    assert client.post("/v1/auth/refresh", json=body).status_code == 401


def test_missing_bad_and_stale_tokens(client: TestClient, owner: Engine) -> None:
    missing = client.get("/v1/me")
    assert (missing.status_code, missing.headers["www-authenticate"]) == (401, "Bearer")
    assert missing.json()["code"] == "auth.required"
    bad = client.get("/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.json()["code"] == "auth.invalid_token"
    tenant = new_tenant(owner)
    tokens = login(client, tenant.admin_email)
    with owner.begin() as conn:
        conn.execute(
            text("UPDATE rai.users SET status = 'disabled' WHERE user_id = :u"),
            {"u": tenant.admin_id},
        )
    assert client.get("/v1/me", headers=auth(tokens)).status_code == 401
    assert client.get("/v1/members", headers=auth(tokens)).status_code == 404


# ---- FR-01.1 acceptance and tenant scoping -------------------------------------------------


def test_a_user_in_tenant_a_cannot_read_any_tenant_b_object(
    client: TestClient, owner: Engine
) -> None:
    a, b = new_tenant(owner), new_tenant(owner)
    tokens = login(client, a.admin_email)
    own = client.get("/v1/members", headers=auth(tokens, a.tenant_id))
    assert [m["email"] for m in own.json()] == [a.admin_email]
    other = client.get("/v1/members", headers=auth(tokens, b.tenant_id))
    assert (other.status_code, other.json()["code"]) == (404, "tenant.not_found")
    missing = client.get("/v1/members", headers=auth(tokens, uuid4()))
    assert missing.json() == other.json() | {"instance": missing.json()["instance"]}
    reach = client.patch(
        f"/v1/members/{b.admin_id}", json={"role": "viewer"}, headers=auth(tokens, a.tenant_id)
    )
    assert (reach.status_code, reach.json()["code"]) == (404, "members.not_found")
    gone = client.delete(f"/v1/members/{b.admin_id}", headers=auth(tokens, a.tenant_id))
    assert gone.status_code == 404


def test_tenant_choice(client: TestClient, owner: Engine) -> None:
    a = new_tenant(owner)
    tokens = login(client, a.admin_email)
    assert client.get("/v1/members", headers=auth(tokens)).status_code == 200  # only tenant
    new_tenant(owner, admin_email=a.admin_email)  # now in two tenants
    several = client.get("/v1/members", headers=auth(tokens))
    assert (several.status_code, several.json()["code"]) == (400, "tenant.required")
    bad = client.get("/v1/members", headers=auth(tokens) | {"X-RAI-Tenant": "acme"})
    assert bad.json()["code"] == "tenant.invalid_header"
    with owner.begin() as conn:
        conn.execute(
            text("UPDATE rai.tenants SET status = 'suspended' WHERE tenant_id = :t"),
            {"t": a.tenant_id},
        )
    assert client.get("/v1/members", headers=auth(tokens, a.tenant_id)).status_code == 404
    lonely = email()  # an active account with no membership anywhere
    with owner.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rai.users (email, display_name, password_hash, status) "
                "VALUES (:e, 'Lonely', :h, 'active')"
            ),
            {"e": lonely, "h": hash_password(PASSWORD)},
        )
    nowhere = client.get("/v1/members", headers=auth(login(client, lonely)))
    assert nowhere.json()["code"] == "tenant.not_found"


# ---- RBAC, invites and role changes ---------------------------------------------------------


def test_roles_are_checked_per_endpoint(client: TestClient, owner: Engine) -> None:
    tenant = new_tenant(owner)
    viewer = member(client, tenant, "viewer")
    engineer = member(client, tenant, "engineer")
    activate(owner, viewer)
    activate(owner, engineer)
    as_viewer = auth(login(client, viewer), tenant.tenant_id)
    assert client.get("/v1/members", headers=as_viewer).status_code == 200
    body = {"email": email(), "display_name": "X", "role": "admin"}
    denied = client.post("/v1/members", json=body, headers=auth(login(client, engineer)))
    assert (denied.status_code, denied.json()["code"]) == (403, "auth.forbidden")
    assert (
        client.patch(
            f"/v1/members/{tenant.admin_id}", json={"role": "viewer"}, headers=as_viewer
        ).status_code
        == 403
    )


def test_invites_answer_the_same_whatever_the_email(client: TestClient, owner: Engine) -> None:
    a, b = new_tenant(owner), new_tenant(owner)
    headers = auth(login(client, a.admin_email), a.tenant_id)
    fresh = email()
    answers = [
        client.post(
            "/v1/members",
            json={"email": e, "display_name": "Someone", "role": role},
            headers=headers,
        )
        for e, role in [
            (fresh, "engineer"),  # new account
            (b.admin_email, "viewer"),  # exists in another tenant
            (fresh.upper(), "admin"),  # already a member here: role unchanged
        ]
    ]
    assert {r.status_code for r in answers} == {202}
    assert len({r.text for r in answers}) == 1
    listed = {m["email"]: m for m in client.get("/v1/members", headers=headers).json()}
    assert listed[fresh]["role"] == "engineer"
    assert (listed[fresh]["status"], listed[b.admin_email]["status"]) == ("invited", "active")
    assert listed[b.admin_email]["display_name"] == "Admin"  # existing account untouched


def test_invite_function_checks_the_caller_itself(owner: Engine, api_url: str) -> None:
    tenant = new_tenant(owner)
    engine = create_engine(api_url)
    invite = text("SELECT rai.invite_member('x@example.test', 'X', 'admin')")
    try:
        with engine.begin() as conn:
            set_context(conn, tenant.tenant_id, "user", str(uuid4()))  # not a member
            with pytest.raises(DBAPIError, match="only an admin"):
                conn.execute(invite)
    finally:
        engine.dispose()


def test_role_changes_are_audited_with_the_signed_in_admin(
    client: TestClient, owner: Engine
) -> None:
    tenant = new_tenant(owner)
    address = member(client, tenant, "engineer")
    headers = auth(login(client, tenant.admin_email), tenant.tenant_id)
    user_id = next(
        m["user_id"]
        for m in client.get("/v1/members", headers=headers).json()
        if m["email"] == address
    )
    changed = client.patch(f"/v1/members/{user_id}", json={"role": "reviewer"}, headers=headers)
    assert (changed.status_code, changed.json()["role"]) == (200, "reviewer")
    with owner.connect() as conn:
        actor = conn.execute(
            text(
                "SELECT actor_type::text, actor_id FROM rai.audit_chain "
                "WHERE tenant_id = :t AND action = 'membership.update' ORDER BY seq DESC LIMIT 1"
            ),
            {"t": tenant.tenant_id},
        ).one()
    assert tuple(actor) == ("user", str(tenant.admin_id))
    self_change = client.patch(
        f"/v1/members/{tenant.admin_id}", json={"role": "viewer"}, headers=headers
    )
    assert (self_change.status_code, self_change.json()["code"]) == (403, "members.self_change")
    assert client.delete(f"/v1/members/{user_id}", headers=headers).status_code == 204
    assert client.delete(f"/v1/members/{user_id}", headers=headers).status_code == 404


def test_a_tenant_keeps_at_least_one_admin(client: TestClient, owner: Engine) -> None:
    tenant = new_tenant(owner)
    second = member(client, tenant, "admin")
    activate(owner, second)
    headers = auth(login(client, second), tenant.tenant_id)
    with owner.begin() as conn:  # the first admin leaves through the operator
        conn.execute(
            text("UPDATE rai.memberships SET role = 'viewer' WHERE user_id = :u"),
            {"u": tenant.admin_id},
        )
    with pytest.raises(DBAPIError, match="at least one admin"), owner.begin() as conn:
        conn.execute(
            text("DELETE FROM rai.memberships WHERE tenant_id = :t AND role = 'admin'"),
            {"t": tenant.tenant_id},
        )
    assert client.get("/v1/members", headers=headers).status_code == 200


# ---- protocol details -----------------------------------------------------------------------


def test_problem_details_for_framework_errors(client: TestClient, owner: Engine) -> None:
    invalid = client.post("/v1/auth/login", json={"email": "a"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "request.invalid"
    assert {e["location"] for e in invalid.json()["errors"]} >= {"body.password"}
    unknown = client.get("/v1/nothing-here")
    assert (unknown.status_code, unknown.json()["code"]) == (404, "http.404")
    huge = client.post("/v1/auth/login", content=b"{}", headers={"Content-Length": str(2 << 20)})
    assert (huge.status_code, huge.json()["code"]) == (413, "request.too_large")
    assert client.get("/healthz").json() == {"status": "ok"}
    tenant = new_tenant(owner)
    token = issue_access_token(Settings(database_url="x", jwt_secret=SECRET), tenant.admin_id)
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---- operator commands -------------------------------------------------------------------------


def test_operator_commands(
    client: TestClient, owner: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.setenv("RAI_DATABASE_URL", DSN)
    monkeypatch.setenv("RAI_NEW_PASSWORD", PASSWORD)
    monkeypatch.setenv("RAI_API_DB_PASSWORD", API_PASSWORD)
    address, slug = email(), f"t-{uuid4().hex[:12]}"
    args = ["admin", "create-tenant", "--slug", slug, "--name", "Demo CU",
            "--admin-email", address, "--admin-name", "Ops"]  # fmt: skip
    created = runner.invoke(cli, args)
    assert created.exit_code == 0, created.output
    assert "(new account)" in created.output
    tokens = login(client, address)
    again = runner.invoke(cli, [*args[:3], f"{slug}-2", *args[4:]])
    assert "(existing account)" in again.output
    assert runner.invoke(cli, args).exit_code == 2  # slug taken

    reset = runner.invoke(cli, ["admin", "set-password", "--email", address])
    assert reset.exit_code == 0, reset.output
    gone = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert gone.status_code == 401  # sessions ended by the reset
    assert runner.invoke(cli, ["admin", "set-password", "--email", email()]).exit_code == 1
    monkeypatch.setenv("RAI_NEW_PASSWORD", "short")
    assert runner.invoke(cli, ["admin", "set-password", "--email", address]).exit_code == 2

    assert runner.invoke(cli, ["db", "set-api-password"]).exit_code == 0
    login(client, address)  # the pool reconnects with the same password
    monkeypatch.setenv("RAI_NEW_PASSWORD", PASSWORD)
    monkeypatch.delenv("RAI_DATABASE_URL")
    assert runner.invoke(cli, ["admin", "set-password", "--email", address]).exit_code == 2
