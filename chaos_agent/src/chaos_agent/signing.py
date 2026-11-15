"""Signed fault requests from the worker (B-24: threat S-03).

The agent acts only on a request the worker signed with its Ed25519 key. The
envelope it receives:

.. code-block:: json

    {"format": "rai-signed-fault/1", "key_id": "<16 hex>",
     "payload": "<base64url of the payload JSON bytes>",
     "signature": "<base64url of the Ed25519 signature>"}

The signature covers ``rai-signed-fault/1`` + a newline + the payload bytes
exactly as sent, so nothing is re-serialised before it is checked. The payload:

.. code-block:: json

    {"format": "rai-fault-request/1", "issued_at": "...Z", "expires_at": "...Z",
     "request": {"type": "...", "target": "...", "run_id": "...", "step_key": "...",
                 "environment": "SANDBOXBANK", "blast_radius": ["..."], ...}}

The agent checks, in order: the key is one it trusts (``RAI_AGENT_WORKER_KEYS``);
the signature; the request was issued at most 30 s in the future, lives at most
120 s and has not expired; and it names the agent's own environment. Each failure
is a :class:`SignatureError` (HTTP 403) that says which check failed. The replay
guard (one fault per run and step) and the agent's second preflight are in
:mod:`chaos_agent.agent`.

The key id is the first 16 hex digits of the SHA-256 of the raw 32-byte public
key, so the agent's configuration lists only public keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ENVELOPE_FORMAT = "rai-signed-fault/1"
PAYLOAD_FORMAT = "rai-fault-request/1"
DOMAIN = (ENVELOPE_FORMAT + "\n").encode("ascii")
MAX_LIFETIME = timedelta(seconds=120)
MAX_SKEW = timedelta(seconds=30)


class SignatureError(ValueError):
    """A request the agent cannot attribute to a trusted worker (403)."""


def b64decode(text: Any, what: str) -> bytes:
    if not isinstance(text, str) or len(text) > 64 * 1024:
        raise SignatureError(f"{what} must be base64url text")
    try:
        # validate=True: Python otherwise skips characters outside the alphabet.
        return base64.b64decode(
            text.replace("-", "+").replace("_", "/") + "=" * (-len(text) % 4), validate=True
        )
    except (binascii.Error, ValueError):
        raise SignatureError(f"{what} is not valid base64url") from None


def key_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


def parse_keys(text: str) -> dict[str, Ed25519PublicKey]:
    """``RAI_AGENT_WORKER_KEYS``: base64 raw Ed25519 public keys, comma-separated."""
    keys: dict[str, Ed25519PublicKey] = {}
    for entry in filter(None, (part.strip() for part in text.split(","))):
        raw = b64decode(entry, "a worker key")
        if len(raw) != 32:
            raise SignatureError("a worker key must be a raw 32-byte Ed25519 public key")
        keys[key_id(raw)] = Ed25519PublicKey.from_public_bytes(raw)
    return keys


def _moment(value: Any, what: str) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise SignatureError(f"{what} is not a timestamp") from None
    if moment.tzinfo is None:
        raise SignatureError(f"{what} must carry a time zone")
    return moment


@dataclass(frozen=True)
class Verified:
    key_id: str
    request: dict[str, Any]  # the fault request, still to be parsed and checked


def verify(
    envelope: Any, keys: dict[str, Ed25519PublicKey], *, environment: str | None, now: datetime
) -> Verified:
    if not keys:
        raise SignatureError("the agent trusts no worker key (RAI_AGENT_WORKER_KEYS)")
    if environment is None:
        raise SignatureError("the agent has no environment (RAI_AGENT_ENVIRONMENT)")
    if not isinstance(envelope, dict) or envelope.get("format") != ENVELOPE_FORMAT:
        raise SignatureError(f"send a signed request ({ENVELOPE_FORMAT})")
    if set(envelope) != {"format", "key_id", "payload", "signature"}:
        raise SignatureError("the envelope has exactly format, key_id, payload and signature")
    public = keys.get(str(envelope["key_id"]))
    if public is None:
        raise SignatureError("the request is signed with a key the agent does not trust")
    payload = b64decode(envelope["payload"], "payload")
    signature = b64decode(envelope["signature"], "signature")
    try:
        public.verify(signature, DOMAIN + payload)
    except InvalidSignature:
        raise SignatureError("the signature does not match") from None
    try:
        body = json.loads(payload)
    except ValueError:
        raise SignatureError("the payload is not JSON") from None
    if not isinstance(body, dict) or body.get("format") != PAYLOAD_FORMAT:
        raise SignatureError(f"the payload must be {PAYLOAD_FORMAT}")
    issued = _moment(body.get("issued_at"), "issued_at")
    expires = _moment(body.get("expires_at"), "expires_at")
    if issued > now + MAX_SKEW:
        raise SignatureError("the request was issued in the future (check the clocks)")
    if not issued < expires <= issued + MAX_LIFETIME:
        raise SignatureError("a signed request may live at most 120 s")
    if now >= expires:
        raise SignatureError("the signed request has expired")
    request = body.get("request")
    if not isinstance(request, dict):
        raise SignatureError("the payload has no request")
    if request.get("environment") != environment:
        raise SignatureError(
            f"the request is for environment {request.get('environment')!r}; "
            f"this agent serves {environment!r}"
        )
    for field in ("run_id", "step_key"):
        if not isinstance(request.get(field), str) or not request[field]:
            raise SignatureError(f"a signed request names its {field}")
    return Verified(str(envelope["key_id"]), request)
