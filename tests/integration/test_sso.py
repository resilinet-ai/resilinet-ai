"""Single sign-on (FR-01.3): sign in through an OpenID Connect provider.

A fake provider signs real RS256 ID tokens and checks PKCE, so every check RAI makes is
exercised: state, nonce, signature, issuer, audience, expiry, algorithm and key. SSO
never creates users: it links an invited or active user by verified email once, then
only by the provider's subject."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from rai.api.oidc import OidcClient, OidcConfig
from tests.integration.apikit import auth, member, needs_db, new_tenant

pytestmark = needs_db

ISSUER = "https://idp.example.test/realms/rai"
CLIENT_ID = "rai-web"
CLIENT_SECRET = "fake-provider-client-secret"  # noqa: S105 - a test double's value
REDIRECT = "http://localhost/v1/auth/sso/callback"


class FakeProvider:
    """Discovery, JWKS and a token endpoint, as httpx transport; ``authorize`` stands in
    for the provider's login page and returns the code it would redirect with."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.codes: dict[str, dict[str, Any]] = {}

    def jwks(self) -> dict[str, Any]:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [public | {"kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def sign(self, claims: dict[str, Any], **header: Any) -> str:
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid} | header)

    def authorize(self, url: str, **claims: Any) -> tuple[str, str]:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == REDIRECT
        assert query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split()
        now = int(time.time())
        token = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "sub-1", "iat": now, "exp": now + 300,
                 "nonce": query["nonce"], "email_verified": True} | claims  # fmt: skip
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": query["code_challenge"], "claims": token}
        return code, query["state"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/certs",
            })  # fmt: skip
        if path.endswith("/certs"):
            return httpx.Response(200, json=self.jwks())
        if path.endswith("/token"):
            basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != f"Basic {basic}":
                return httpx.Response(401, json={"error": "invalid_client"})
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            grant = self.codes.pop(form["code"], None)
            if grant is None or form.get("redirect_uri") != REDIRECT:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})  # PKCE
            claims = grant["claims"]
            token = claims.pop("__raw__", None) or self.sign(claims)
            return httpx.Response(200, json={"id_token": token, "access_token": "x"})
        return httpx.Response(404)


@pytest.fixture
def provider(client: TestClient) -> Iterator[FakeProvider]:
    fake = FakeProvider()
    config = OidcConfig(issuer=ISSUER, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                        redirect_uri=REDIRECT, ui_url="/", name="Example SSO")  # fmt: skip
    state: Any = client.app.state  # type: ignore[attr-defined]
    state.oidc = OidcClient(config, http=httpx.Client(transport=httpx.MockTransport(fake.handle)))
    yield fake
    state.oidc = None
    client.cookies.clear()


def sign_in(client: TestClient, provider: FakeProvider, **claims: Any) -> str:
    """Start, let the provider authorize, call back; returns the UI's fragment."""
    started = client.get("/v1/auth/sso/start", follow_redirects=False)
    assert started.status_code == 303, started.text
    assert "httponly" in started.headers["set-cookie"].lower()
    code, state = provider.authorize(started.headers["location"], **claims)
    back = client.get("/v1/auth/sso/callback", params={"code": code, "state": state},
                      follow_redirects=False)  # fmt: skip
    assert back.status_code == 303
    location = back.headers["location"]
    assert location.startswith("/#")
    return location.split("#", 1)[1]


def tokens(client: TestClient, fragment: str) -> dict[str, Any]:
    assert fragment.startswith("sso="), fragment
    response = client.post("/v1/auth/sso/exchange", json={"code": fragment[4:]})
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_an_invited_member_signs_in_and_stays_linked_to_their_subject(
    client: TestClient, owner: Engine, provider: FakeProvider
) -> None:
    assert client.get("/v1/auth/sso").json() == {"enabled": True, "name": "Example SSO"}
    tenant = new_tenant(owner)
    invited = member(client, tenant, "engineer")  # invited, has never set a password
    subject = f"sub-{secrets.token_hex(4)}"
    pair = tokens(client, sign_in(client, provider, sub=subject, email=invited.upper()))
    me = client.get("/v1/me", headers=auth(pair)).json()
    assert me["email"] == invited
    assert [t["role"] for t in me["tenants"]] == ["engineer"]

    # The provider may change the email later; the subject keeps the same account.
    again = tokens(client, sign_in(client, provider, sub=subject, email="new@else.test"))
    assert client.get("/v1/me", headers=auth(again)).json()["user_id"] == me["user_id"]
    with owner.connect() as conn:
        linked = conn.execute(
            text("SELECT u.status::text, i.issuer FROM rai.user_identities i "
                 "JOIN rai.users u USING (user_id) WHERE i.subject = :s"), {"s": subject},
        ).one()  # fmt: skip
    assert (linked.status, linked.issuer) == ("active", ISSUER)


@pytest.mark.parametrize(
    "claims",
    [{"email_verified": False}, {"email_verified": "true"}, {"email": None}],
    ids=["unverified", "verified-as-text", "no-email"],
)
def test_an_email_the_provider_did_not_verify_never_links(
    client: TestClient, owner: Engine, provider: FakeProvider, claims: dict[str, Any]
) -> None:
    invited = member(client, new_tenant(owner), "viewer")
    fragment = sign_in(client, provider, sub=secrets.token_hex(6), **({"email": invited} | claims))
    assert fragment == "sso_error=sso.no_account"


def test_sso_never_creates_users_nor_signs_in_disabled_ones(
    client: TestClient, owner: Engine, provider: FakeProvider
) -> None:
    unknown = sign_in(client, provider, sub=secrets.token_hex(6), email="nobody@example.test")
    assert unknown == "sso_error=sso.no_account"
    invited = member(client, new_tenant(owner), "viewer")
    with owner.begin() as conn:
        conn.execute(text("UPDATE rai.users SET status = 'disabled' WHERE email = :e"),
                     {"e": invited})  # fmt: skip
    assert sign_in(client, provider, sub=secrets.token_hex(6), email=invited) == (
        "sso_error=sso.no_account")  # fmt: skip


def test_a_handoff_code_works_once(
    client: TestClient, owner: Engine, provider: FakeProvider
) -> None:
    invited = member(client, new_tenant(owner), "viewer")
    fragment = sign_in(client, provider, sub=secrets.token_hex(6), email=invited)
    assert tokens(client, fragment)["refresh_token"]
    replay = client.post("/v1/auth/sso/exchange", json={"code": fragment[4:]})
    assert (replay.status_code, replay.json()["code"]) == (401, "sso.invalid_code")
    forged = client.post("/v1/auth/sso/exchange", json={"code": secrets.token_urlsafe(32)})
    assert forged.status_code == 401


def test_a_callback_that_is_not_ours_is_refused(
    client: TestClient, owner: Engine, provider: FakeProvider
) -> None:
    invited = member(client, new_tenant(owner), "viewer")
    started = client.get("/v1/auth/sso/start", follow_redirects=False)
    code, _ = provider.authorize(started.headers["location"], email=invited)
    forged = client.get("/v1/auth/sso/callback", params={"code": code, "state": "attacker"},
                        follow_redirects=False)  # fmt: skip
    assert forged.headers["location"] == "/#sso_error=sso.state_mismatch"

    client.cookies.clear()  # another browser: no attempt in progress
    code, state = provider.authorize(started.headers["location"], email=invited)
    elsewhere = client.get("/v1/auth/sso/callback", params={"code": code, "state": state},
                           follow_redirects=False)  # fmt: skip
    assert elsewhere.headers["location"] == "/#sso_error=sso.expired"
    refused = client.get("/v1/auth/sso/callback", params={"error": "access_denied"},
                         follow_redirects=False)  # fmt: skip
    assert refused.headers["location"] == "/#sso_error=sso.provider_refused"


def _forged(provider: FakeProvider, kind: str, now: int) -> dict[str, Any]:
    """Claims (or a raw token) the provider's real answer would never have."""
    if kind == "hs256":  # signed with the client secret, as in the algorithm confusion attack
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "iat": now, "exp": now + 60}
        return {"__raw__": jwt.encode(claims, CLIENT_SECRET, algorithm="HS256")}
    if kind == "other-key":
        other = FakeProvider()
        return {"__raw__": other.sign({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x",
                                       "iat": now, "exp": now + 60})}  # fmt: skip
    return {
        "issuer": {"iss": "https://evil.example.test"},
        "audience": {"aud": "someone-else"},
        "expired": {"exp": now - 600, "iat": now - 900},
        "nonce": {"nonce": "replayed"},
        "azp": {"aud": [CLIENT_ID, "someone-else"], "azp": "someone-else"},
    }[kind]


@pytest.mark.parametrize("kind", ["issuer", "audience", "expired", "nonce", "azp", "hs256",
                                  "other-key"])  # fmt: skip
def test_an_id_token_that_is_not_the_providers_answer_is_refused(
    client: TestClient, owner: Engine, provider: FakeProvider, kind: str
) -> None:
    invited = member(client, new_tenant(owner), "viewer")
    fragment = sign_in(client, provider, email=invited, **_forged(provider, kind, int(time.time())))
    assert fragment == "sso_error=sso.invalid_token"


def test_without_configuration_sso_is_off(client: TestClient) -> None:
    assert client.get("/v1/auth/sso").json() == {"enabled": False, "name": None}
    off = client.get("/v1/auth/sso/start", follow_redirects=False)
    assert (off.status_code, off.json()["code"]) == (404, "sso.not_configured")
