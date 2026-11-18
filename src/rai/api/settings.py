"""API settings from the environment (B-16). Secrets are never logged or echoed."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from rai.connectors.secrets import SecretError, SecretRef
from rai.core.db import sqlalchemy_url
from rai.core.evidence import EvidenceKeyError, EvidenceSigner

API_DATABASE_URL_ENV = "RAI_API_DATABASE_URL"
JWT_SECRET_ENV = "RAI_JWT_SECRET"  # noqa: S105 - a variable name, not a secret
JWT_SECRET_FILE_ENV = "RAI_JWT_SECRET_FILE"  # noqa: S105 - a variable name
EVIDENCE_KEY_ENV = "RAI_EVIDENCE_SIGNING_KEY"  # B-35: a secret reference to a PEM key
MIN_SECRET_BYTES = 32


class SettingsError(RuntimeError):
    """The API cannot start with this configuration."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    jwt_secret: str = field(repr=False)
    access_ttl: timedelta = timedelta(minutes=15)
    refresh_ttl: timedelta = timedelta(days=7)
    issuer: str = "rai"
    max_body_bytes: int = 1 << 20
    login_attempts: int = 5  # failed logins per email per window
    login_window: timedelta = timedelta(minutes=15)
    key_requests_per_minute: int = 120  # per API key (threat D-03)
    evidence_signer: EvidenceSigner | None = field(default=None, repr=False)  # B-35: FR-13.2

    def __post_init__(self) -> None:
        if len(self.jwt_secret.encode("utf-8")) < MIN_SECRET_BYTES:
            raise SettingsError(f"the JWT secret must be at least {MIN_SECRET_BYTES} bytes")

    @classmethod
    def from_env(cls) -> Settings:
        url = os.environ.get(API_DATABASE_URL_ENV)
        if not url:
            raise SettingsError(
                f"set {API_DATABASE_URL_ENV} to the rai-db URL for the rai_api login role"
            )
        secret = os.environ.get(JWT_SECRET_ENV)
        secret_file = os.environ.get(JWT_SECRET_FILE_ENV)
        if secret_file:
            try:
                secret = Path(secret_file).read_text(encoding="utf-8").strip()
            except OSError as error:
                raise SettingsError(f"cannot read {JWT_SECRET_FILE_ENV}: {error}") from None
        if not secret:
            raise SettingsError(f"set {JWT_SECRET_ENV} or {JWT_SECRET_FILE_ENV}")
        return cls(
            database_url=sqlalchemy_url(url),
            jwt_secret=secret,
            evidence_signer=evidence_signer_from_env(),
        )


def evidence_signer_from_env() -> EvidenceSigner | None:
    """The evidence signing key named by RAI_EVIDENCE_SIGNING_KEY (``file:`` or ``env:``),
    or None: bundles are then exported unsigned, and ``rai verify`` says so."""
    ref = os.environ.get(EVIDENCE_KEY_ENV, "").strip()
    if not ref:
        return None
    try:
        return EvidenceSigner.from_pem(SecretRef.parse(ref).resolve().reveal().encode("ascii"))
    except (SecretError, EvidenceKeyError, UnicodeEncodeError) as error:
        raise SettingsError(f"{EVIDENCE_KEY_ENV}: {error}") from None
