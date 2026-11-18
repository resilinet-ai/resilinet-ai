"""Evidence bundles: deterministic bytes and a manifest that catches changes (B-34: FR-13.1)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
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
