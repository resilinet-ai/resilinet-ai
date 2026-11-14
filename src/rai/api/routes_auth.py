"""Sign-in, token refresh, sign-out and "who am I" (B-16, Data Model section 8)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from rai.api.context import Principal, app_transaction, engine_of, principal, settings_of
from rai.api.problems import Problem
from rai.api.ratelimit import FailureLimiter
from rai.api.tokens import issue_access_token, new_refresh_token, refresh_hash
from rai.core.passwords import verify_password

router = APIRouter(prefix="/v1", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=200)


class TokenPair(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth token type, not a secret
    expires_in: int
    refresh_token: str


class TenantOut(BaseModel):
    tenant_id: UUID
    slug: str
    name: str
    role: str


class MeOut(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    tenants: list[TenantOut]


def _pair(request: Request, user_id: UUID, refresh_token: str) -> TokenPair:
    settings = settings_of(request)
    return TokenPair(
        access_token=issue_access_token(settings, user_id),
        expires_in=int(settings.access_ttl.total_seconds()),
        refresh_token=refresh_token,
    )


def _limiters(request: Request) -> tuple[FailureLimiter, FailureLimiter]:
    state: Any = request.app.state
    return state.login_by_email, state.login_by_address


@router.post("/auth/login")
def login(body: LoginRequest, request: Request) -> TokenPair:
    """Same answer for an unknown email, a wrong password and a disabled or invited
    account (threat S-01); repeated failures are throttled per email and per address."""
    by_email, by_address = _limiters(request)
    email_key = "email:" + body.email.strip().lower()
    address_key = "address:" + (request.client.host if request.client else "unknown")
    wait = by_email.retry_after(email_key) or by_address.retry_after(address_key)
    if wait is not None:
        raise Problem(
            429, "auth.too_many_attempts", "Too many failed sign-ins; try again later",
            headers={"Retry-After": str(wait)},
        )  # fmt: skip
    refresh_token, digest = new_refresh_token()
    with app_transaction(engine_of(request)) as conn:
        row = conn.execute(
            text("SELECT user_id, password_hash, status FROM rai.auth_lookup(:e)"),
            {"e": body.email.strip()},
        ).first()
        matched = verify_password(row.password_hash if row else None, body.password)
        if row is not None and matched and row.status == "active":
            user_id = UUID(str(row.user_id))
            conn.execute(text("SELECT rai.auth_record_login(:u)"), {"u": user_id})
            conn.execute(
                text("SELECT rai.refresh_issue(:u, :h, :ttl)"),
                {"u": user_id, "h": digest, "ttl": settings_of(request).refresh_ttl},
            )
        else:
            user_id = None
    if user_id is None:
        by_email.fail(email_key)
        by_address.fail(address_key)
        raise Problem(401, "auth.invalid_credentials", "Email or password is wrong")
    by_email.clear(email_key)
    return _pair(request, user_id, refresh_token)


@router.post("/auth/refresh")
def refresh(body: RefreshRequest, request: Request) -> TokenPair:
    """Rotate: the old refresh token stops working. Re-using an old one ends the session."""
    new_token, digest = new_refresh_token()
    with app_transaction(engine_of(request)) as conn:
        # Committed even when it fails, so a detected re-use really revokes the family.
        user = conn.execute(
            text("SELECT rai.refresh_rotate(:old, :new)"),
            {"old": refresh_hash(body.refresh_token), "new": digest},
        ).scalar_one()
    if user is None:
        raise Problem(401, "auth.invalid_refresh_token", "Sign in again")
    return _pair(request, UUID(str(user)), new_token)


@router.post("/auth/logout", status_code=204)
def logout(
    body: RefreshRequest, request: Request, caller: Annotated[Principal, Depends(principal)]
) -> Response:
    with app_transaction(engine_of(request)) as conn:
        conn.execute(
            text("SELECT rai.refresh_revoke(:u, :h)"),
            {"u": caller.user_id, "h": refresh_hash(body.refresh_token)},
        )
    return Response(status_code=204)


def _tenants(request: Request, user_id: UUID) -> tuple[Any, list[TenantOut]]:
    with app_transaction(engine_of(request)) as conn:
        profile = conn.execute(
            text("SELECT user_id, email, display_name FROM rai.user_profile(:u)"), {"u": user_id}
        ).first()
        rows = conn.execute(
            text("SELECT tenant_id, slug, name, role FROM rai.user_tenants(:u)"), {"u": user_id}
        ).all()
    if profile is None:  # disabled since the token was issued
        raise Problem(401, "auth.invalid_token", "The access token is not valid or has expired")
    return profile, [
        TenantOut(tenant_id=r.tenant_id, slug=r.slug, name=r.name, role=r.role) for r in rows
    ]


@router.get("/me")
def me(request: Request, caller: Annotated[Principal, Depends(principal)]) -> MeOut:
    profile, tenants = _tenants(request, caller.user_id)
    return MeOut(
        user_id=profile.user_id,
        email=profile.email,
        display_name=profile.display_name,
        tenants=tenants,
    )


@router.get("/tenants")
def my_tenants(
    request: Request, caller: Annotated[Principal, Depends(principal)]
) -> list[TenantOut]:
    """Tenants the caller belongs to. New tenants are created by the operator
    (``rai admin create-tenant``), because ``rai_app`` cannot create tenants (threat E-04)."""
    return _tenants(request, caller.user_id)[1]
