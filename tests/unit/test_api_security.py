"""Passwords, tokens, settings and the login limiter (B-16; threats S-01, D-03)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from typer.testing import CliRunner

from rai.api.ratelimit import FailureLimiter
from rai.api.settings import Settings, SettingsError
from rai.api.tokens import (
    ALGORITHM,
    InvalidToken,
    issue_access_token,
    new_refresh_token,
    read_access_token,
    refresh_hash,
)
from rai.cli.main import app
from rai.core.evidence import EvidenceSigner
from rai.core.passwords import WeakPassword, hash_password, verify_password

SECRET = secrets.token_urlsafe(48)  # a fresh signing key per test run
SETTINGS = Settings(database_url="postgresql+psycopg://x/y", jwt_secret=SECRET)


def test_argon2id_hashes_verify_and_never_match_without_a_hash() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert verify_password(stored, "correct horse battery")
    assert not verify_password(stored, "wrong horse battery")
    assert not verify_password(None, "anything at all")  # unknown email: same work, no match
    assert not verify_password("not-a-hash", "anything at all")


@pytest.mark.parametrize(
    ("password", "message"),
    [("short", "at least 12"), ("x" * 257, "at most 256"), (" " * 12, "blank")],
)
def test_password_policy(password: str, message: str) -> None:
    with pytest.raises(WeakPassword, match=message):
        hash_password(password)


def test_access_tokens_round_trip_and_name_only_the_user() -> None:
    user = uuid4()
    token = issue_access_token(SETTINGS, user)
    assert read_access_token(SETTINGS, token) == user
    claims = jwt.decode(token, SECRET, algorithms=[ALGORITHM], issuer="rai")
    assert set(claims) == {"iss", "sub", "typ", "iat", "exp", "jti"}
    assert claims["exp"] - claims["iat"] == 15 * 60


def test_bad_access_tokens_are_refused() -> None:
    user = uuid4()
    expired = issue_access_token(SETTINGS, user, now=datetime.now(UTC) - timedelta(hours=1))
    other = Settings(database_url="x", jwt_secret=SECRET.upper())  # a different key
    forged = issue_access_token(other, user)
    now = int(datetime.now(UTC).timestamp())
    base = {"iss": "rai", "sub": str(user), "iat": now, "exp": now + 60, "jti": "j"}
    wrong_type = jwt.encode(base | {"typ": "refresh"}, SECRET, algorithm=ALGORITHM)
    missing = jwt.encode(base, SECRET, algorithm=ALGORITHM)
    unsigned = jwt.encode(base | {"typ": "access"}, None, algorithm="none")
    for token in (expired, forged, wrong_type, missing, unsigned, "garbage"):
        with pytest.raises(InvalidToken):
            read_access_token(SETTINGS, token)


def test_refresh_tokens_are_random_and_stored_only_as_hashes() -> None:
    first, first_hash = new_refresh_token()
    second, _ = new_refresh_token()
    assert first.startswith("rairt_")
    assert first != second
    assert first_hash == refresh_hash(first)
    assert len(first_hash) == 64
    assert first not in first_hash


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("RAI_API_DATABASE_URL", "RAI_JWT_SECRET", "RAI_JWT_SECRET_FILE",
                 "RAI_EVIDENCE_SIGNING_KEY"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SettingsError, match="RAI_API_DATABASE_URL"):
        Settings.from_env()
    monkeypatch.setenv("RAI_API_DATABASE_URL", "postgresql://rai_api@localhost/rai")
    with pytest.raises(SettingsError, match="RAI_JWT_SECRET"):
        Settings.from_env()
    monkeypatch.setenv("RAI_JWT_SECRET", "too-short")
    with pytest.raises(SettingsError, match="at least 32 bytes"):
        Settings.from_env()
    secret_file = tmp_path / "jwt"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")
    monkeypatch.setenv("RAI_JWT_SECRET_FILE", str(secret_file))
    settings = Settings.from_env()
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.jwt_secret == SECRET
    assert SECRET not in repr(settings)
    monkeypatch.setenv("RAI_JWT_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(SettingsError, match="cannot read"):
        Settings.from_env()


def test_serve_refuses_to_start_without_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAI_API_DATABASE_URL", raising=False)
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 2
    assert "RAI_API_DATABASE_URL" in result.output


def test_failure_limiter_slides_and_clears() -> None:
    now = [1_000.0]
    limiter = FailureLimiter(2, 60, clock=lambda: now[0])
    assert limiter.retry_after("a") is None
    limiter.fail("a", "b")
    limiter.fail("a")
    assert limiter.retry_after("a") == 60
    assert limiter.retry_after("b") is None
    assert limiter.retry_after("b", "a") == 60  # any key over the limit blocks
    now[0] += 30
    assert limiter.retry_after("a") == 30
    now[0] += 30
    assert limiter.retry_after("a") is None  # failures older than the window drop out
    limiter.fail("a")
    limiter.fail("a")
    limiter.clear("a")
    assert limiter.retry_after("a") is None


def test_the_evidence_signing_key_comes_from_a_secret_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B-35: RAI_EVIDENCE_SIGNING_KEY names the key; without it bundles are unsigned."""
    monkeypatch.setenv("RAI_API_DATABASE_URL", "postgresql://rai_api@localhost/rai")
    monkeypatch.setenv("RAI_JWT_SECRET", SECRET)
    monkeypatch.delenv("RAI_JWT_SECRET_FILE", raising=False)
    monkeypatch.delenv("RAI_EVIDENCE_SIGNING_KEY", raising=False)
    assert Settings.from_env().evidence_signer is None
    signer = EvidenceSigner.generate()
    key_file = tmp_path / "evidence.pem"
    key_file.write_bytes(signer.pem())
    monkeypatch.setenv("RAI_EVIDENCE_SIGNING_KEY", f"file:{key_file.as_posix()}")
    settings = Settings.from_env()
    assert settings.evidence_signer is not None
    assert settings.evidence_signer.key_id == signer.key_id
    assert "PRIVATE" not in repr(settings)
    monkeypatch.setenv("RAI_EVIDENCE_SIGNING_KEY", key_file.as_posix())  # not a reference
    with pytest.raises(SettingsError, match="RAI_EVIDENCE_SIGNING_KEY"):
        Settings.from_env()
    key_file.write_text("not a key", encoding="utf-8")
    monkeypatch.setenv("RAI_EVIDENCE_SIGNING_KEY", f"file:{key_file.as_posix()}")
    with pytest.raises(SettingsError, match="readable PEM"):
        Settings.from_env()
