"""Secret references (FR-02.3): credentials are named, never stored.

A connector config holds a *reference* such as ``env:LEDGER_PASSWORD`` or
``file:/run/secrets/ledger_password`` (a Docker secret). The value is read only
when a connection is opened, is wrapped in :class:`Secret` so it prints as
``****``, and is scrubbed from any error message. The database, logs and evidence
bundles therefore only ever see the reference (threat model I-04).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# file: takes an absolute POSIX path (Docker secrets) or a Windows drive path (development).
_REF = re.compile(r"^(env):([A-Za-z_][A-Za-z0-9_]*)$|^(file):(/.+|[A-Za-z]:[/\\].+)$")
_URL_PASSWORD = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^:/@\s]*):(?P<pw>[^@\s]*)@")
_KEY_VALUE = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api_key)\s*=\s*('[^']*'|\S+)")
MASK = "****"


class SecretError(ValueError):
    """A secret reference that is malformed or cannot be resolved."""


@dataclass(frozen=True, slots=True)
class SecretRef:
    scheme: str  # "env" or "file"
    target: str  # variable name or absolute file path

    @classmethod
    def parse(cls, text: str) -> SecretRef:
        match = _REF.match(text.strip())
        if not match:
            raise SecretError(
                f"secret reference {text!r} must be env:VARIABLE or file:/absolute/path"
            )
        if match[1]:
            return cls("env", match[2])
        return cls("file", match[4])

    def __str__(self) -> str:
        return f"{self.scheme}:{self.target}"

    def resolve(self) -> Secret:
        if self.scheme == "env":
            value = os.environ.get(self.target)
            if value is None:
                raise SecretError(f"environment variable {self.target} named by {self} is not set")
            return Secret(value, self)
        path = Path(self.target)
        if not path.is_file():
            raise SecretError(f"secret file {self.target} named by {self} does not exist")
        return Secret(path.read_text(encoding="utf-8").rstrip("\r\n"), self)


class Secret:
    """A resolved secret. Its value is only available through :meth:`reveal`."""

    __slots__ = ("_ref", "_value")

    def __init__(self, value: str, ref: SecretRef) -> None:
        self._value = value
        self._ref = ref

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({self._ref}, {MASK})"

    __str__ = __repr__


def scrub(text: str, *secrets: Secret | str) -> str:
    """Remove secret values, URL passwords and ``password=...`` pairs from ``text``."""
    for secret in secrets:
        value = secret.reveal() if isinstance(secret, Secret) else secret
        if value:
            text = text.replace(value, MASK)
    text = _URL_PASSWORD.sub(lambda m: f"{m['scheme']}{m['user']}:{MASK}@", text)
    return _KEY_VALUE.sub(lambda m: f"{m[1]}={MASK}", text)
