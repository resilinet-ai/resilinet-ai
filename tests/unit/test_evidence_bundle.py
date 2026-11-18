"""Evidence bundles: deterministic bytes, a manifest and a signature that catch changes
(B-34: FR-13.1; B-35: FR-13.2)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from rai.core import evidence

RUN = UUID("0193a5b0-0000-7000-8000-000000000001")
MEMBERS = {
    "run.json": {"run_id": RUN, "state": "passed", "rto": Decimal("12.500"),
                 "finished_at": datetime(2026, 11, 18, 9, 0, 1, 5, tzinfo=UTC)},
    "observations.json": [{"rule": "not_null:txn_id", "outcome": "pass", "note": "naïve café"}],
}  # fmt: skip


def members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {n: archive.read(n) for n in archive.namelist()}


def rezip(contents: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_two_builds_of_the_same_members_are_byte_identical() -> None:
    first = evidence.build(RUN, MEMBERS)
    reordered = dict(reversed(list(MEMBERS.items())))
    second = evidence.build(RUN, reordered)
    assert first.data == second.data
    assert first.manifest_sha256 == second.manifest_sha256
    assert (first.name, first.files) == (f"rai-evidence-{RUN}", 2)
    with zipfile.ZipFile(io.BytesIO(first.data)) as archive:
        infos = archive.infolist()
    assert [i.filename.split("/", 1)[1] for i in infos] == [
        "observations.json", "run.json", "manifest.json"]  # fmt: skip
    for info in infos:  # nothing that depends on the exporting host or time
        assert (info.date_time, info.create_system, info.compress_type) == (
            evidence.ZIP_DATE, 3, zipfile.ZIP_STORED)  # fmt: skip


def test_members_are_canonical_json() -> None:
    files = members(evidence.build(RUN, MEMBERS).data)
    run = files[f"rai-evidence-{RUN}/run.json"]
    assert (
        run
        == (
            '{"finished_at":"2026-11-18T09:00:01.000005Z","rto":"12.500",'
            f'"run_id":"{RUN}","state":"passed"}}\n'
        ).encode()
    )
    assert "naïve café".encode() in files[f"rai-evidence-{RUN}/observations.json"]
    plus_two = datetime(2026, 11, 18, 11, 0, 1, 5, tzinfo=timezone(timedelta(hours=2)))
    assert evidence.canonical_json(plus_two) == b'"2026-11-18T09:00:01.000005Z"\n'
    with pytest.raises(ValueError, match="time zone"):
        evidence.canonical_json(datetime(2026, 11, 18))  # no zone: the point


def test_the_manifest_lists_every_member_with_its_digest() -> None:
    bundle = evidence.build(RUN, MEMBERS)
    files = members(bundle.data)
    raw = files[f"rai-evidence-{RUN}/manifest.json"]
    manifest = json.loads(raw)
    assert manifest["format"] == "rai-evidence/1"
    assert [f["path"] for f in manifest["files"]] == ["observations.json", "run.json"]
    for entry in manifest["files"]:
        body = files[f"rai-evidence-{RUN}/{entry['path']}"]
        assert (entry["sha256"], entry["bytes"]) == (evidence.sha256(body), len(body))
    assert bundle.manifest_sha256 == evidence.sha256(raw)
    check = evidence.verify(bundle.data)
    assert (check.ok, check.problems, check.manifest_sha256) == (True, (), bundle.manifest_sha256)
    with pytest.raises(ValueError, match="manifest"):
        evidence.build(RUN, {"manifest.json": {}})


def test_verify_names_every_change() -> None:
    files = members(evidence.build(RUN, MEMBERS).data)
    root = f"rai-evidence-{RUN}"
    edited = files | {f"{root}/run.json": files[f"{root}/run.json"].replace(b"passed", b"failed")}
    assert evidence.verify(rezip(edited)).problems == ("run.json: does not match its SHA-256",)
    added = files | {f"{root}/extra.json": b"{}\n"}
    assert evidence.verify(rezip(added)).problems == ("extra.json: not in the manifest",)
    lost = {k: v for k, v in files.items() if not k.endswith("observations.json")}
    assert evidence.verify(rezip(lost)).problems == ("observations.json: listed but missing",)
    unlisted = {k: v for k, v in files.items() if not k.endswith("manifest.json")}
    assert not evidence.verify(rezip(unlisted)).ok
    assert evidence.verify(b"not a zip").problems == ("not a ZIP file",)
    flat = {k.split("/", 1)[1]: v for k, v in files.items()}
    assert evidence.verify(rezip(flat)).problems == ("members must sit in one folder",)


# ---- signatures (B-35: FR-13.2) --------------------------------------------------------------


SIGNER = evidence.EvidenceSigner.generate()


def trusted(*signers: evidence.EvidenceSigner) -> list[Any]:
    return [evidence.public_key_from_text(s.public_key) for s in signers]


def test_a_signed_bundle_is_still_deterministic_and_its_manifest_still_checks() -> None:
    first, second = evidence.build(RUN, MEMBERS, SIGNER), evidence.build(RUN, MEMBERS, SIGNER)
    assert first.data == second.data  # Ed25519 signatures are deterministic
    assert first.key_id == SIGNER.key_id
    assert evidence.verify(first.data).ok  # signature.json is outside the manifest
    names = list(members(first.data))
    assert names[-2:] == [f"rai-evidence-{RUN}/manifest.json", f"rai-evidence-{RUN}/signature.json"]
    with pytest.raises(ValueError, match="signature"):
        evidence.build(RUN, {"signature.json": {}})


def test_only_a_valid_signature_by_a_trusted_key_is_trusted() -> None:
    signed = evidence.build(RUN, MEMBERS, SIGNER).data
    assert evidence.check_signature(signed, trusted(SIGNER)).status == "trusted"
    untrusted = evidence.check_signature(signed, trusted(evidence.EvidenceSigner.generate()))
    assert (untrusted.status, untrusted.key_id) == ("untrusted", SIGNER.key_id)
    assert evidence.check_signature(signed).status == "untrusted"  # no key given
    assert evidence.check_signature(evidence.build(RUN, MEMBERS).data).status == "unsigned"


def test_a_changed_manifest_or_signature_is_invalid() -> None:
    files = members(evidence.build(RUN, MEMBERS, SIGNER).data)
    root = f"rai-evidence-{RUN}"
    # Someone edits a member and rewrites the manifest to match: the signature breaks.
    run = files[f"{root}/run.json"].replace(b"passed", b"failed")
    manifest = json.loads(files[f"{root}/manifest.json"])
    for entry in manifest["files"]:
        if entry["path"] == "run.json":
            entry.update(sha256=evidence.sha256(run), bytes=len(run))
    forged = files | {f"{root}/run.json": run,
                      f"{root}/manifest.json": evidence.canonical_json(manifest)}  # fmt: skip
    assert evidence.verify(rezip(forged)).ok  # the manifest alone cannot tell
    check = evidence.check_signature(rezip(forged), trusted(SIGNER))
    assert (check.status, check.detail) == ("invalid", "the signature is for another manifest")
    # ... and re-signing it with their own key makes it valid, but not trusted.
    other = evidence.EvidenceSigner.generate()
    resigned = forged | {f"{root}/signature.json": evidence.canonical_json(
        other.sign(evidence.canonical_json(manifest)))}  # fmt: skip
    assert evidence.check_signature(rezip(resigned), trusted(SIGNER)).status == "untrusted"
    # A signature over the right manifest hash, but the wrong bytes, does not verify.
    envelope = json.loads(files[f"{root}/signature.json"])
    envelope["signature"] = other.sign(files[f"{root}/manifest.json"])["signature"]
    swapped = files | {f"{root}/signature.json": evidence.canonical_json(envelope)}
    assert evidence.check_signature(rezip(swapped), trusted(SIGNER)).detail == (
        "the signature does not verify")  # fmt: skip
    garbled = files | {f"{root}/signature.json": b"{}"}
    assert evidence.check_signature(rezip(garbled)).status == "invalid"


def test_keys_must_be_ed25519() -> None:
    assert evidence.EvidenceSigner.from_pem(SIGNER.pem()).key_id == SIGNER.key_id
    with pytest.raises(evidence.EvidenceKeyError):
        evidence.EvidenceSigner.from_pem(b"not a key")
    with pytest.raises(evidence.EvidenceKeyError):
        evidence.public_key_from_text("c2hvcnQ=")  # 5 bytes
    assert "PRIVATE" not in repr(SIGNER)
