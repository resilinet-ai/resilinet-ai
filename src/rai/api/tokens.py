"""Access and refresh tokens (B-16, Architecture section 7).

* Access token: a JWT (HS256) valid for 15 minutes, naming only the user. Tenant
  and role are looked up on every request, so removing a member or changing a role
  takes effect at once, not when the token expires.
* Refresh token: 32 random bytes, shown to the client once; the database keeps
  only its SHA-256. It rotates on every use (see migration 0002).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

import jwt

from rai.api.settings import Settings

ALGORITHM = "HS256"
REFRESH_PREFIX = "rairt_"


class InvalidToken(Exception):
    """Missing, malformed, expired or forged token."""


def issue_access_token(settings: Settings, user_id: UUID, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    claims = {
        "iss": settings.issuer,
        "sub": str(user_id),
        "typ": "access",
        "iat": int(moment.timestamp()),
        "exp": int((moment + settings.access_ttl).timestamp()),
        "jti": uuid4().hex,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def read_access_token(settings: Settings, token: str) -> UUID:
    """The user id of a valid access token."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            issuer=settings.issuer,
            options={"require": ["exp", "iat", "iss", "sub", "typ", "jti"]},
        )
        if claims["typ"] != "access":
            raise InvalidToken("not an access token")
        return UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError) as error:
        raise InvalidToken(str(error)) from None


def new_refresh_token() -> tuple[str, str]:
    """(token for the client, SHA-256 hex stored in the database)."""
    token = REFRESH_PREFIX + secrets.token_urlsafe(32)
    return token, refresh_hash(token)


def refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
