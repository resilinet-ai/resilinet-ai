"""Single sign-on (FR-01.3): sign in through the deployment's OpenID Connect provider.

1. ``GET /v1/auth/sso`` tells the web UI whether SSO is on, and the button's label.
2. ``GET /v1/auth/sso/start`` redirects the browser to the provider, with a signed,
   HttpOnly cookie that remembers this attempt (state, nonce, PKCE verifier).
3. ``GET /v1/auth/sso/callback`` checks the provider's answer, signs the user in, and
   redirects to the web UI with a one-time code in the URL fragment (``#sso=``), which
   never reaches a server or a Referer. A refusal redirects with ``#sso_error=<code>``
   and nothing else: the reason is logged, not shown.
4. ``POST /v1/auth/sso/exchange`` turns the code into the usual token pair, once, within
   two minutes.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from rai.api.context import app_transaction, engine_of, settings_of
from rai.api.oidc import (
    COOKIE,
    COOKIE_PATH,
    FLOW_TTL,
    OidcClient,
    OidcError,
    handoff_hash,
    new_handoff,
)
from rai.api.problems import Problem
from rai.api.routes_auth import TokenPair, _pair
from rai.api.tokens import new_refresh_token

router = APIRouter(prefix="/v1/auth/sso", tags=["auth"])
log = logging.getLogger("rai.api.sso")


class SsoOut(BaseModel):
    enabled: bool
    name: str | None = None


class ExchangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=20, max_length=100)


def _client(request: Request) -> OidcClient:
    client: OidcClient | None = getattr(request.app.state, "oidc", None)
    if client is None:
        raise Problem(404, "sso.not_configured", "Single sign-on is not configured here")
    return client


def _back(client: OidcClient, fragment: str) -> RedirectResponse:
    response = RedirectResponse(f"{client.config.ui_url}#{fragment}", status_code=303)
    response.delete_cookie(COOKIE, path=COOKIE_PATH, secure=client.config.secure_cookie,
                           httponly=True, samesite="lax")  # fmt: skip
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("")
def sso(request: Request) -> SsoOut:
    client: OidcClient | None = getattr(request.app.state, "oidc", None)
    return (
        SsoOut(enabled=False) if client is None else SsoOut(enabled=True, name=client.config.name)
    )


@router.get("/start")
def start(request: Request) -> Response:
    client = _client(request)
    try:
        url, cookie = client.start(settings_of(request).jwt_secret)
    except OidcError as error:
        log.warning("sso start refused", extra={"reason": str(error)})
        return _back(client, f"sso_error={error.code}")
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(COOKIE, cookie, max_age=int(FLOW_TTL.total_seconds()), path=COOKIE_PATH,
                        secure=client.config.secure_cookie, httponly=True,
                        samesite="lax")  # fmt: skip
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    client = _client(request)
    if error or not code or not state:
        log.warning("sso refused by the provider", extra={"reason": error or "no code"})
        return _back(client, "sso_error=sso.provider_refused")
    try:
        identity = client.finish(settings_of(request).jwt_secret, request.cookies.get(COOKIE),
                                 state, code)  # fmt: skip
    except OidcError as refused:
        log.warning("sso sign-in refused", extra={"reason": str(refused)})
        return _back(client, f"sso_error={refused.code}")
    handoff, digest = new_handoff()
    with app_transaction(engine_of(request)) as conn:
        user = conn.execute(
            text("SELECT rai.oidc_sign_in(:i, :s, :e, :v)"),
            {"i": identity.issuer, "s": identity.subject, "e": identity.email,
             "v": identity.email_verified},
        ).scalar_one()  # fmt: skip
        if user is not None:
            conn.execute(text("SELECT rai.sso_handoff_issue(:u, :h)"), {"u": user, "h": digest})
    if user is None:
        log.warning("sso: no RAI user for this identity",
                    extra={"reason": "unknown, unverified email, or disabled"})  # fmt: skip
        return _back(client, "sso_error=sso.no_account")
    return _back(client, f"sso={handoff}")


@router.post("/exchange")
def exchange(body: ExchangeIn, request: Request) -> TokenPair:
    """The one-time code from the callback, for an access and a refresh token."""
    refresh_token, digest = new_refresh_token()
    with app_transaction(engine_of(request)) as conn:
        user = conn.execute(
            text("SELECT rai.sso_handoff_redeem(:h)"), {"h": handoff_hash(body.code)}
        ).scalar_one()
        if user is not None:
            conn.execute(
                text("SELECT rai.refresh_issue(:u, :h, :ttl)"),
                {"u": user, "h": digest, "ttl": settings_of(request).refresh_ttl},
            )
    if user is None:
        raise Problem(401, "sso.invalid_code", "Sign in again")
    return _pair(request, UUID(str(user)), refresh_token)
