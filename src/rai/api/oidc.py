"""OpenID Connect sign-in (FR-01.3): the authorization code flow with PKCE.

One identity provider per deployment, configured by the operator:

* ``RAI_OIDC_ISSUER``: the provider's issuer URL (``https://``; ``http://`` only for
  localhost). Its endpoints come from ``/.well-known/openid-configuration``.
* ``RAI_OIDC_CLIENT_ID`` and ``RAI_OIDC_CLIENT_SECRET`` (a secret reference such as
  ``env:NAME`` or ``file:/path``; a confidential client).
* ``RAI_OIDC_REDIRECT_URI``: this API's ``/v1/auth/sso/callback``, as registered.
* ``RAI_OIDC_UI_URL`` (default ``/``): where the browser returns after signing in.
* ``RAI_OIDC_NAME`` (default ``single sign-on``): the button's label.

What is checked (threats S-01 and S-04):

* **The request is ours.** ``state``, the ``nonce`` and the PKCE verifier live in a
  signed, HttpOnly cookie for ten minutes; the callback compares the state in constant
  time, and the ID token must carry the nonce.
* **The ID token is the provider's.** Its signature verifies with a key from the
  provider's JWKS, with an asymmetric algorithm only (never ``none`` or HMAC); ``iss``
  is the issuer, ``aud`` holds the client ID (and ``azp`` names it when there are
  several audiences), and ``exp`` and ``iat`` are checked with a minute of leeway.
* **Who it is.** The database links an unknown subject only by a *verified* email, and
  only to a user a tenant admin invited (``rai.oidc_sign_in``).

The ID token, access token and code are never logged or stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from rai.connectors.secrets import SecretError, SecretRef

COOKIE = "rai_sso"
COOKIE_PATH = "/v1/auth/sso"
FLOW_TTL = timedelta(minutes=10)
LEEWAY = 60  # seconds
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA"]
TIMEOUT = 10.0


class OidcError(Exception):
    """A sign-in that must not succeed. ``code`` is safe to show; details are not."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class OidcConfigError(RuntimeError):
    """The API cannot start with this SSO configuration."""


def _local(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _checked_url(name: str, value: str, *, query: bool = False) -> str:
    parts = urlsplit(value)
    secure = parts.scheme == "https" or (parts.scheme == "http" and _local(parts.hostname))
    if secure and parts.hostname and (query or not parts.query) and not parts.fragment:
        return value
    raise OidcConfigError(f"{name} must be an https:// URL (http:// only for localhost), "
                          "without a fragment" + ("" if query else " or query"))  # fmt: skip


@dataclass(frozen=True, slots=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    ui_url: str = "/"
    name: str = "single sign-on"

    def __post_init__(self) -> None:
        _checked_url("RAI_OIDC_ISSUER", self.issuer)
        _checked_url("RAI_OIDC_REDIRECT_URI", self.redirect_uri)
        if not self.client_id or len(self.client_id) > 255:
            raise OidcConfigError("RAI_OIDC_CLIENT_ID must be 1 to 255 characters")
        if not self.client_secret:
            raise OidcConfigError("RAI_OIDC_CLIENT_SECRET resolves to an empty secret")
        if not (self.ui_url.startswith("/") and not self.ui_url.startswith("//")):
            _checked_url("RAI_OIDC_UI_URL", self.ui_url)

    @property
    def secure_cookie(self) -> bool:
        return self.redirect_uri.startswith("https://")

    @classmethod
    def from_env(cls) -> OidcConfig | None:
        issuer = os.environ.get("RAI_OIDC_ISSUER", "").strip()
        if not issuer:
            return None
        missing = [n for n in ("RAI_OIDC_CLIENT_ID", "RAI_OIDC_CLIENT_SECRET",
                               "RAI_OIDC_REDIRECT_URI") if not os.environ.get(n)]  # fmt: skip
        if missing:
            raise OidcConfigError(f"RAI_OIDC_ISSUER is set, so set {', '.join(missing)} too")
        try:
            secret = SecretRef.parse(os.environ["RAI_OIDC_CLIENT_SECRET"]).resolve().reveal()
        except SecretError as error:
            raise OidcConfigError(f"RAI_OIDC_CLIENT_SECRET: {error}") from None
        return cls(
            issuer=issuer.rstrip("/"),
            client_id=os.environ["RAI_OIDC_CLIENT_ID"].strip(),
            client_secret=secret,
            redirect_uri=os.environ["RAI_OIDC_REDIRECT_URI"].strip(),
            ui_url=os.environ.get("RAI_OIDC_UI_URL", "/").strip() or "/",
            name=os.environ.get("RAI_OIDC_NAME", "single sign-on").strip()[:60] or "single sign-on",
        )


@dataclass(frozen=True, slots=True)
class Identity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class OidcClient:
    """The provider's metadata and keys, fetched when first needed and cached."""

    def __init__(self, config: OidcConfig, http: httpx.Client | None = None) -> None:
        self.config = config
        self.http = http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "rai-api"})
        self._lock = threading.RLock()  # _key reads the metadata while holding it
        self._metadata: dict[str, Any] | None = None
        self._keys: jwt.PyJWKSet | None = None

    # ---- provider metadata ------------------------------------------------------------------

    def _get(self, url: str) -> dict[str, Any]:
        try:
            response = self.http.get(url)
        except httpx.HTTPError as error:
            raise OidcError("sso.provider_unreachable", str(error)) from None
        if response.status_code != 200:
            raise OidcError("sso.provider_unreachable", f"{url}: {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise OidcError("sso.provider_invalid", f"{url} is not a JSON object")
        return body

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            if self._metadata is None:
                found = self._get(f"{self.config.issuer}/.well-known/openid-configuration")
                if found.get("issuer") != self.config.issuer:
                    raise OidcError("sso.provider_invalid", "the discovery issuer differs")
                for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    if not isinstance(found.get(name), str):
                        raise OidcError("sso.provider_invalid", f"no {name}")
                    try:
                        _checked_url(name, found[name], query=True)
                    except OidcConfigError as error:
                        raise OidcError("sso.provider_invalid", str(error)) from None
                self._metadata = found
            return self._metadata

    def _key(self, kid: str | None) -> jwt.PyJWK:
        for attempt in range(2):  # refetch once: the provider may have rotated its keys
            with self._lock:
                if self._keys is None or attempt:
                    self._keys = jwt.PyJWKSet.from_dict(self._get(self.metadata()["jwks_uri"]))
                keys = self._keys
            matching = [k for k in keys.keys if kid is None or k.key_id == kid]
            if len(matching) == 1:
                return matching[0]
        raise OidcError("sso.invalid_token", f"no single signing key for kid {kid!r}")

    # ---- the flow ---------------------------------------------------------------------------

    def start(self, signing_secret: str, *, now: datetime | None = None) -> tuple[str, str]:
        """The provider's authorization URL, and the cookie that remembers this attempt."""
        moment = now or datetime.now(UTC)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        cookie = jwt.encode(
            {"aud": "rai-sso", "state": state, "nonce": nonce, "verifier": verifier,
             "iat": moment, "exp": moment + FLOW_TTL},
            signing_secret, algorithm="HS256",
        )  # fmt: skip
        query = urlencode({
            "response_type": "code", "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri, "scope": "openid email profile",
            "state": state, "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode("ascii")).digest()),
            "code_challenge_method": "S256",
        })  # fmt: skip
        endpoint = self.metadata()["authorization_endpoint"]
        separator = "&" if urlsplit(endpoint).query else "?"
        return f"{endpoint}{separator}{query}", cookie

    def finish(self, signing_secret: str, cookie: str | None, state: str, code: str) -> Identity:
        """Check the callback against its cookie, exchange the code, verify the ID token."""
        if not cookie:
            raise OidcError("sso.expired", "no sign-in in progress in this browser")
        try:
            flow = jwt.decode(cookie, signing_secret, algorithms=["HS256"], audience="rai-sso")
        except jwt.PyJWTError:
            raise OidcError("sso.expired", "the sign-in cookie is invalid or expired") from None
        if not hmac.compare_digest(str(flow["state"]), state):
            raise OidcError("sso.state_mismatch")
        token = self._exchange(code, str(flow["verifier"]))
        return self._verify(token, str(flow["nonce"]))

    def _exchange(self, code: str, verifier: str) -> str:
        try:
            response = self.http.post(
                self.metadata()["token_endpoint"],
                data={"grant_type": "authorization_code", "code": code,
                      "redirect_uri": self.config.redirect_uri, "code_verifier": verifier},
                auth=(self.config.client_id, self.config.client_secret),
                headers={"Accept": "application/json"},
            )  # fmt: skip
        except httpx.HTTPError as error:
            raise OidcError("sso.provider_unreachable", str(error)) from None
        if response.status_code != 200:
            raise OidcError("sso.code_refused", f"token endpoint answered {response.status_code}")
        try:
            id_token = response.json()["id_token"]
        except (ValueError, KeyError, TypeError):
            raise OidcError("sso.provider_invalid", "no id_token in the token response") from None
        if not isinstance(id_token, str):
            raise OidcError("sso.provider_invalid", "id_token is not a string")
        return id_token

    def _verify(self, id_token: str, nonce: str) -> Identity:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise OidcError("sso.invalid_token", "not a JWT") from None
        if header.get("alg") not in ALGORITHMS:
            raise OidcError("sso.invalid_token", f"algorithm {header.get('alg')!r} not allowed")
        key = self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token, key=key, algorithms=[str(header["alg"])],
                audience=self.config.client_id, issuer=self.config.issuer, leeway=LEEWAY,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )  # fmt: skip
        except jwt.PyJWTError as error:
            raise OidcError("sso.invalid_token", str(error)) from None
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("sso.invalid_token", "the nonce differs")
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if len(audiences) > 1 and claims.get("azp") != self.config.client_id:
            raise OidcError("sso.invalid_token", "several audiences and azp is not this client")
        subject = str(claims["sub"])
        if not 1 <= len(subject) <= 255:
            raise OidcError("sso.invalid_token", "the subject is empty or too long")
        email = claims.get("email")
        return Identity(
            issuer=self.config.issuer, subject=subject,
            email=email.strip() if isinstance(email, str) and email.strip() else None,
            email_verified=claims.get("email_verified") is True,
        )  # fmt: skip


def new_handoff() -> tuple[str, str]:
    """A one-time code for the web UI, and the SHA-256 the database keeps."""
    code = secrets.token_urlsafe(32)
    return code, hashlib.sha256(code.encode("ascii")).hexdigest()


def handoff_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
