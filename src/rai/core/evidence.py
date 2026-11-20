"""Evidence bundles: a deterministic, signed export of one finished run (B-34: FR-13.1;
B-35: FR-13.2; threats T-02, T-04).

A bundle is a ZIP file whose members sit under ``rai-evidence-<run_id>/``:

* ``bundle.json``: what the bundle is, its scope, the audit-chain anchor, and the key
  that signs it;
* ``run.json``: the run, with the pins it executed from (plan, definitions, digests);
* ``steps.json``, ``observations.json``, ``lineage.json``, ``recovery.json``: the
  run's insert-only results;
* ``approvals.json``: the scenario approvals the run was pinned to;
* ``audit.json``: the run's own audit-chain entries, with their hashes;
* ``report.md``: the same run for a person to read, with its limitations statement
  (B-45; :mod:`rai.core.report`);
* ``manifest.json``: the SHA-256 and size of every member above;
* ``signature.json``: an Ed25519 signature over ``manifest.json``, when the API has
  an evidence signing key. It is the only member the manifest does not list.

**Deterministic.** Every JSON member is canonical JSON (sorted keys, no spaces, UTF-8,
one trailing newline), and ``report.md`` is UTF-8 text with LF line endings. Members
are stored, not compressed, in a fixed order with a fixed date, permissions and host
system, so two exports of the same run are byte-identical on any platform. Nothing in a
bundle depends on when it was exported: a run's results never change once it has
finished, the anchor is the run's own last audit entry, not the chain's moving head,
and Ed25519 signatures are deterministic (RFC 8032), so the same key gives the same
signature.

**Anchor (T-02).** ``bundle.json`` records the ``seq`` and ``record_hash`` of the
run's last audit entry. A later rewrite of the audit chain at or before that entry
changes that hash, so comparing it with the live chain exposes the rewrite.

**Signature (T-04).** The signature covers ``DOMAIN + manifest.json``, and the
manifest covers every other member, so any change after export breaks one or the
other. A signature proves who exported the bundle only to a verifier who trusts the
signer's public key from somewhere other than the bundle itself (``rai verify --key``).
All of it is tamper-evident, not tamper-proof (NFR-AUD-2).

No secrets: pins hold secret references, never values (threat I-04), and synthetic
data is not included, only its digests from the lineage edges.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

FORMAT = "rai-evidence/1"
SIGNATURE_FORMAT = "rai-evidence-signature/1"
MANIFEST = "manifest.json"
SIGNATURE = "signature.json"
DOMAIN = b"rai-evidence-manifest/1\x00"  # a signature here is never valid for anything else
ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # the earliest date a ZIP can hold: no export time
STATEMENT = (
    "Tamper-evident, not tamper-proof (NFR-AUD-2): the manifest and its signature show "
    "whether a member changed after export, and the anchor whether the audit chain was "
    "rewritten at or before the run's last entry. Neither shows that the recorded facts "
    "were true."
)


def plain(value: Any) -> Any:
    """JSON-ready: UUIDs and decimals as strings, times in UTC with microseconds."""
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("evidence times must carry a time zone")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value


def encode(content: Any) -> bytes:
    """A member's bytes: text as UTF-8 with LF line endings and one final newline, anything
    else as canonical JSON."""
    if isinstance(content, str):
        text = content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        return (text + "\n").encode("utf-8")
    return canonical_json(content)


def canonical_json(value: Any) -> bytes:
    """The bytes of a bundle member: sorted keys, no spaces, UTF-8, a final newline."""
    text = json.dumps(plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EvidenceKeyError(ValueError):
    """A key that is not an Ed25519 key, or not readable."""


def key_id_of(public: bytes) -> str:
    """The first 16 hex characters of the raw public key's SHA-256."""
    return hashlib.sha256(public).hexdigest()[:16]


def public_key_from_text(text: str) -> Ed25519PublicKey:
    """A raw 32-byte Ed25519 public key in standard base64 (what ``evidence-keygen`` prints)."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise EvidenceKeyError("a public key is 32 bytes in base64") from None
    if len(raw) != 32:  # the Ed25519 key size
        raise EvidenceKeyError("a public key is 32 bytes in base64")
    return Ed25519PublicKey.from_public_bytes(raw)


class EvidenceSigner:
    """The API's evidence key. The private key never leaves this object."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key = base64.b64encode(raw).decode("ascii")
        self.key_id = key_id_of(raw)

    def __repr__(self) -> str:
        return f"EvidenceSigner(key_id={self.key_id})"

    @classmethod
    def generate(cls) -> EvidenceSigner:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: bytes) -> EvidenceSigner:
        try:
            key = load_pem_private_key(pem, password=None)
        except (ValueError, TypeError):
            raise EvidenceKeyError("the evidence signing key is not a readable PEM key") from None
        if not isinstance(key, Ed25519PrivateKey):
            raise EvidenceKeyError("the evidence signing key must be an Ed25519 private key")
        return cls(key)

    def pem(self) -> bytes:
        return self._key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())

    def sign(self, manifest: bytes) -> dict[str, str]:
        return {
            "format": SIGNATURE_FORMAT,
            "algorithm": "ed25519",
            "key_id": self.key_id,
            "public_key": self.public_key,
            "manifest_sha256": sha256(manifest),
            "signature": base64.b64encode(self._key.sign(DOMAIN + manifest)).decode("ascii"),
        }


@dataclass(frozen=True)
class Bundle:
    name: str  # the folder inside the ZIP, and the file name without ``.zip``
    data: bytes  # the ZIP file
    manifest_sha256: str  # SHA-256 of manifest.json: what the signature covers
    files: int  # members listed in the manifest
    key_id: str | None  # the signing key, or None for an unsigned bundle


def signature_note(signer: EvidenceSigner | None) -> dict[str, str] | None:
    """What ``bundle.json`` says about the signature (it cannot hold the signature itself)."""
    if signer is None:
        return None
    return {"algorithm": "ed25519", "key_id": signer.key_id, "member": SIGNATURE}


def build(run_id: UUID, members: Mapping[str, Any], signer: EvidenceSigner | None = None) -> Bundle:
    """The ZIP for ``members`` (name -> JSON-ready content, or text), its manifest and
    signature."""
    if MANIFEST in members or SIGNATURE in members:
        raise ValueError(f"{MANIFEST} and {SIGNATURE} are written by build()")
    name = f"rai-evidence-{run_id}"
    encoded = {path: encode(content) for path, content in sorted(members.items())}
    manifest = canonical_json({
        "format": FORMAT,
        "algorithm": "sha256",
        "files": [{"path": p, "sha256": sha256(b), "bytes": len(b)} for p, b in encoded.items()],
    })  # fmt: skip
    written = [*encoded.items(), (MANIFEST, manifest)]
    if signer is not None:
        written.append((SIGNATURE, canonical_json(signer.sign(manifest))))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, data in written:
            info = zipfile.ZipInfo(f"{name}/{path}", date_time=ZIP_DATE)
            info.create_system = 3  # Unix, whatever the exporting host is
            info.external_attr = 0o100644 << 16  # a regular file, rw-r--r--
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    key_id = signer.key_id if signer else None
    return Bundle(name, buffer.getvalue(), sha256(manifest), len(encoded), key_id)


@dataclass(frozen=True)
class Check:
    ok: bool
    problems: tuple[str, ...]
    manifest_sha256: str | None = None


def _open(data: bytes) -> tuple[str, dict[str, bytes]] | str:
    """The bundle's folder and members, or what is wrong with it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "not a ZIP file"
    with archive:
        names = archive.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        if len(roots) != 1 or any("/" not in n for n in names):
            return "members must sit in one folder"
        return roots.pop(), {n.split("/", 1)[1]: archive.read(n) for n in names}


def verify(data: bytes) -> Check:
    """Whether a bundle's members match its manifest exactly: none changed, added or lost.
    ``signature.json`` is the one member the manifest does not list (see check_signature)."""
    opened = _open(data)
    if isinstance(opened, str):
        return Check(False, (opened,))
    root, contents = opened
    contents.pop(SIGNATURE, None)
    raw = contents.pop(MANIFEST, None)
    if raw is None:
        return Check(False, (f"{MANIFEST} is missing",))
    try:
        listed = {f["path"]: f for f in json.loads(raw)["files"]}
    except (ValueError, KeyError, TypeError):
        return Check(False, (f"{MANIFEST} is not a manifest",))
    problems: list[str] = []
    for path in sorted(listed.keys() - contents.keys()):
        problems.append(f"{path}: listed but missing")
    for path in sorted(contents.keys() - listed.keys()):
        problems.append(f"{path}: not in the manifest")
    for path in sorted(listed.keys() & contents.keys()):
        body, entry = contents[path], listed[path]
        if sha256(body) != entry.get("sha256") or len(body) != entry.get("bytes"):
            problems.append(f"{path}: does not match its SHA-256")
    if not root.startswith("rai-evidence-"):
        problems.append(f"{root}: not an evidence bundle folder")
    return Check(not problems, tuple(problems), sha256(raw))


SignatureStatus = Literal["trusted", "untrusted", "unsigned", "invalid"]


@dataclass(frozen=True)
class SignatureCheck:
    status: SignatureStatus
    key_id: str | None
    detail: str


def check_signature(data: bytes, trusted: Iterable[Ed25519PublicKey] = ()) -> SignatureCheck:
    """Whether ``signature.json`` is a valid Ed25519 signature over ``manifest.json``, and
    whether its key is one of ``trusted`` (keys the verifier got from outside the bundle).

    ``trusted`` the signature is valid and the key is trusted; ``untrusted`` it is valid
    but by a key the verifier was not given; ``unsigned`` there is no signature;
    ``invalid`` the signature does not verify, or is malformed."""
    opened = _open(data)
    if isinstance(opened, str):
        return SignatureCheck("invalid", None, opened)
    _, contents = opened
    raw_signature, manifest = contents.get(SIGNATURE), contents.get(MANIFEST)
    if raw_signature is None:
        return SignatureCheck("unsigned", None, "the bundle has no signature.json")
    if manifest is None:
        return SignatureCheck("invalid", None, f"{MANIFEST} is missing")
    try:
        envelope = json.loads(raw_signature)
        if envelope["format"] != SIGNATURE_FORMAT or envelope["algorithm"] != "ed25519":
            return SignatureCheck("invalid", None, "unknown signature format")
        public = public_key_from_text(envelope["public_key"])
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (ValueError, KeyError, TypeError, binascii.Error):
        return SignatureCheck("invalid", None, "signature.json is malformed")
    raw_public = public.public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = key_id_of(raw_public)
    if envelope.get("key_id") != key_id:
        return SignatureCheck("invalid", key_id, "the key id does not match the public key")
    if envelope.get("manifest_sha256") != sha256(manifest):
        return SignatureCheck("invalid", key_id, "the signature is for another manifest")
    try:
        public.verify(signature, DOMAIN + manifest)
    except InvalidSignature:
        return SignatureCheck("invalid", key_id, "the signature does not verify")
    trusted_raw = {k.public_bytes(Encoding.Raw, PublicFormat.Raw) for k in trusted}
    if raw_public in trusted_raw:
        return SignatureCheck("trusted", key_id, f"signed by trusted key {key_id}")
    return SignatureCheck("untrusted", key_id,
                          f"valid, but key {key_id} is not one you trust (--key)")  # fmt: skip
