"""``rai verify``: check an evidence bundle offline (B-35: FR-13.2; threat T-04).

Three checks, none of which needs RAI or a network:

1. **Manifest.** Every member matches its SHA-256 in ``manifest.json``; none is
   added or missing.
2. **Signature.** ``signature.json`` is a valid Ed25519 signature over the manifest,
   by a key you trust: one you pass with ``--key`` (base64, or ``@file``) and got from
   somewhere other than the bundle. A valid signature by a key you did not pass proves
   nothing about who signed it, so it fails unless trusted.
3. **Anchor.** The run's last audit entry (``seq``, ``record_hash``) is printed, to
   compare with the live chain (``rai audit verify``); this command cannot reach it.

Exit codes: 0 verified; 1 the bundle failed a check (changed, unsigned, untrusted or
invalid signature); 2 the bundle or a key could not be read.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Annotated, Any

import typer

from rai.core import evidence


def _key(text: str) -> Any:
    raw = Path(text[1:]).read_text(encoding="utf-8") if text.startswith("@") else text
    return evidence.public_key_from_text(raw)


def _bundle_json(data: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            name = next(n for n in archive.namelist() if n.endswith("/bundle.json"))
            loaded: dict[str, Any] = json.loads(archive.read(name))
            return loaded
    except (zipfile.BadZipFile, StopIteration, ValueError):
        return {}


def verify_command(
    bundle: Annotated[Path, typer.Argument(help="The evidence bundle (.zip).")],
    key: Annotated[
        list[str] | None,
        typer.Option("--key", help="A trusted public key: base64, or @file. Repeatable."),
    ] = None,
    allow_unsigned: Annotated[
        bool, typer.Option(help="Pass a bundle with no signature (the manifest is still checked).")
    ] = False,
) -> None:
    """Check an evidence bundle: its manifest, its signature and its audit anchor."""
    try:
        data = bundle.read_bytes()
        trusted = [_key(k) for k in key or []]
    except (OSError, evidence.EvidenceKeyError) as error:
        typer.echo(f"ERROR {error}", err=True)
        raise typer.Exit(2) from None

    failed = False
    manifest = evidence.verify(data)
    if manifest.ok:
        info = _bundle_json(data)
        count = sum((info.get("counts") or {}).values()) if info else 0
        typer.echo(f"OK      manifest: every member matches (sha256 {manifest.manifest_sha256})")
        if count:
            typer.echo(f"        {count:,} records in the bundle")
    else:
        failed = True
        for problem in manifest.problems:
            typer.echo(f"FAIL    manifest: {problem}")

    signature = evidence.check_signature(data, trusted)
    if signature.status == "trusted":
        typer.echo(f"OK      signature: {signature.detail}")
    elif signature.status == "unsigned" and allow_unsigned:
        typer.echo("WARN    signature: none (--allow-unsigned)")
    else:
        failed = True
        hint = " (pass --allow-unsigned to accept)" if signature.status == "unsigned" else ""
        typer.echo(f"FAIL    signature: {signature.detail}{hint}")

    anchor = (_bundle_json(data) if manifest.ok else {}).get("anchor")
    if anchor:
        typer.echo(f"INFO    anchor: audit entry seq {anchor['seq']}, record_hash "
                   f"{anchor['record_hash']}; compare with the live chain")  # fmt: skip
    typer.echo(f"{'FAILED' if failed else 'VERIFIED'} {bundle.name}")
    raise typer.Exit(1 if failed else 0)
