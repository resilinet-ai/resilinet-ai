"""Argon2id password hashing (B-16, threat S-01).

Parameters follow RFC 9106's second recommended option (t=3, m=64 MiB, p=4).
``verify`` always does the work of one Argon2id verification, even when there is
no stored hash (unknown email, invited user), so response time does not reveal
whether an account exists.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 256

_hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)
# Verified against when there is nothing real to verify: same cost, never matches.
_DUMMY_HASH = _hasher.hash("not a password: timing equaliser")


class WeakPassword(ValueError):
    """A new password that does not meet the policy."""


def check_policy(password: str) -> None:
    """NIST SP 800-63B style: length, not composition rules."""
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"password must be at most {MAX_LENGTH} characters")
    if not password.strip():
        raise WeakPassword("password must not be blank")


def hash_password(password: str) -> str:
    check_policy(password)
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True only if ``password`` matches ``stored_hash``; constant work either way."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password[:MAX_LENGTH]) and bool(
            stored_hash
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
