"""Evidence bundles: a deterministic export of one finished run (B-34: FR-13.1; T-02, T-04).

A bundle is a ZIP file whose members sit under ``rai-evidence-<run_id>/``:

* ``bundle.json``: what the bundle is, its scope and the audit-chain anchor;
* ``run.json``: the run, with the pins it executed from (plan, definitions, digests);
* ``steps.json``, ``observations.json``, ``lineage.json``, ``recovery.json``: the
  run's insert-only results;
* ``approvals.json``: the scenario approvals the run was pinned to;
* ``audit.json``: the run's own audit-chain entries, with their hashes;
* ``manifest.json``: the SHA-256 and size of every other member.

**Deterministic.** Every member is canonical JSON (sorted keys, no spaces, UTF-8,
one trailing newline). Members are stored, not compressed, in a fixed order with a
fixed date, permissions and host system, so two exports of the same run are
byte-identical on any platform. Nothing in a bundle depends on when it was exported:
a run's results never change once it has finished, and the anchor is the run's own
last audit entry, not the chain's moving head.

**Anchor (T-02).** ``bundle.json`` records the ``seq`` and ``record_hash`` of the
run's last audit entry. A later rewrite of the audit chain at or before that entry
changes that hash, so comparing it with the live chain exposes the rewrite. It is
tamper-evident, not tamper-proof (NFR-AUD-2), and the manifest is not signed yet
(B-35: FR-13.2).

No secrets: pins hold secret references, never values (threat I-04), and synthetic
data is not included, only its digests from the lineage edges.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

FORMAT = "rai-evidence/1"
MANIFEST = "manifest.json"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # the earliest date a ZIP can hold: no export time
STATEMENT = (
    "Tamper-evident, not tamper-proof (NFR-AUD-2): the manifest shows whether a member "
    "changed after export, and the anchor whether the audit chain was rewritten at or "
    "before the run's last entry. Neither shows that the recorded facts were true."
)


def _plain(value: Any) -> Any:
    """JSON-ready: UUIDs and decimals as strings, times in UTC with microseconds."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("evidence times must carry a time zone")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value


def canonical_json(value: Any) -> bytes:
    """The bytes of a bundle member: sorted keys, no spaces, UTF-8, a final newline."""
    text = json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Bundle:
    name: str  # the folder inside the ZIP, and the file name without ``.zip``
    data: bytes  # the ZIP file
    manifest_sha256: str  # SHA-256 of manifest.json: what B-35 will sign
    files: int  # members listed in the manifest


def build(run_id: UUID, members: Mapping[str, Any]) -> Bundle:
    """The ZIP for ``members`` (name -> JSON-ready content), with its manifest."""
    if MANIFEST in members:
        raise ValueError(f"{MANIFEST} is written by build()")
    name = f"rai-evidence-{run_id}"
    encoded = {path: canonical_json(content) for path, content in sorted(members.items())}
    manifest = canonical_json({
        "format": FORMAT,
        "algorithm": "sha256",
        "files": [{"path": p, "sha256": sha256(b), "bytes": len(b)} for p, b in encoded.items()],
    })  # fmt: skip
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, data in [*encoded.items(), (MANIFEST, manifest)]:
            info = zipfile.ZipInfo(f"{name}/{path}", date_time=ZIP_DATE)
            info.create_system = 3  # Unix, whatever the exporting host is
            info.external_attr = 0o100644 << 16  # a regular file, rw-r--r--
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    return Bundle(name, buffer.getvalue(), sha256(manifest), len(encoded))


@dataclass(frozen=True)
class Check:
    ok: bool
    problems: tuple[str, ...]
    manifest_sha256: str | None = None


def verify(data: bytes) -> Check:
    """Whether a bundle's members match its manifest exactly: none changed, added or lost."""
    problems: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return Check(False, ("not a ZIP file",))
    with archive:
        names = archive.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        if len(roots) != 1 or any("/" not in n for n in names):
            return Check(False, ("members must sit in one folder",))
        root = roots.pop()
        contents = {n.split("/", 1)[1]: archive.read(n) for n in names}
    raw = contents.pop(MANIFEST, None)
    if raw is None:
        return Check(False, (f"{MANIFEST} is missing",))
    try:
        listed = {f["path"]: f for f in json.loads(raw)["files"]}
    except (ValueError, KeyError, TypeError):
        return Check(False, (f"{MANIFEST} is not a manifest",))
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
