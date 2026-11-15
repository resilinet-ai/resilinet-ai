"""Project-scoped API keys for CI (B-20: FR-06.1; threat S-02).

A key looks like ``rai_<prefix>_<secret>``:

* ``prefix``: 12 characters from ``a-z2-7``, unique. It identifies the key in
  lists and logs without revealing it.
* ``secret``: 32 random bytes, base64url without padding (43 characters).

Only the SHA-256 of the secret is stored, so the database cannot give a key back;
the secret is shown once, when the key is created. A 256-bit random secret needs
no slow hash: there is nothing to guess.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from typing import Literal

TOKEN_PREFIX = "rai_"  # noqa: S105 - the public marker of the format, not a secret
Scope = Literal["runs:start", "runs:read"]
SCOPES: tuple[Scope, ...] = ("runs:start", "runs:read")
MAX_DAYS = 90
_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_TOKEN = re.compile(r"^rai_([a-z2-7]{12})_([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class NewKey:
    prefix: str
    secret_hash: str
    token: str = field(repr=False)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def generate() -> NewKey:
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    secret = secrets.token_urlsafe(32)
    return NewKey(prefix, hash_secret(secret), f"{TOKEN_PREFIX}{prefix}_{secret}")


def is_key(token: str) -> bool:
    """Bearer tokens in the API key format (JWTs start with ``eyJ``)."""
    return token.startswith(TOKEN_PREFIX)


def parse(token: str) -> tuple[str, str] | None:
    """``(prefix, secret)``, or None if the token is not a well-formed key."""
    match = _TOKEN.fullmatch(token)
    return (match.group(1), match.group(2)) if match else None


def matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash)
