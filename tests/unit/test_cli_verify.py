"""``rai verify`` and ``rai admin evidence-keygen`` (B-35: FR-13.2)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from uuid import UUID

from typer.testing import CliRunner

from rai.cli.main import app
from rai.core import evidence

RUN = UUID("0193a5b0-0000-7000-8000-000000000001")
MEMBERS = {
    "bundle.json": {"anchor": {"seq": 1204, "record_hash": "8d" * 32},
                    "counts": {"steps": 2, "observations": 72}},
    "run.json": {"run_id": RUN, "state": "failed"},
}  # fmt: skip
runner = CliRunner()


def write(tmp_path: Path, data: bytes, name: str = "bundle.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def verify(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["verify", *args])
    return result.exit_code, result.output


def test_a_signed_bundle_verifies_with_its_key(tmp_path: Path) -> None:
    signer = evidence.EvidenceSigner.generate()
    path = write(tmp_path, evidence.build(RUN, MEMBERS, signer).data)
    code, out = verify(str(path), "--key", signer.public_key)
    assert code == 0, out
    assert f"OK      signature: signed by trusted key {signer.key_id}" in out
    assert "74 records in the bundle" in out
    assert f"anchor: audit entry seq 1204, record_hash {'8d' * 32}" in out
    assert out.rstrip().endswith("VERIFIED bundle.zip")

    key_file = tmp_path / "evidence.pub"
    key_file.write_text(signer.public_key + "\n", "utf-8")
    assert verify(str(path), "--key", f"@{key_file}")[0] == 0

    code, out = verify(str(path))  # no key: valid, but nothing says whose
    assert code == 1
    assert f"key {signer.key_id} is not one you trust" in out


def test_unsigned_and_changed_bundles_fail(tmp_path: Path) -> None:
    unsigned = write(tmp_path, evidence.build(RUN, MEMBERS).data)
    code, out = verify(str(unsigned))
    assert (code, "pass --allow-unsigned to accept" in out) == (1, True)
    code, out = verify(str(unsigned), "--allow-unsigned")
    assert (code, "WARN    signature: none" in out) == (0, True)

    signer = evidence.EvidenceSigner.generate()
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(evidence.build(RUN, MEMBERS, signer).data)) as archive:
        contents = {n: archive.read(n) for n in archive.namelist()}
    run = f"rai-evidence-{RUN}/run.json"
    contents[run] = contents[run].replace(b"failed", b"passed")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    changed = write(tmp_path, buffer.getvalue(), "changed.zip")
    code, out = verify(str(changed), "--key", signer.public_key)
    assert code == 1
    assert "FAIL    manifest: run.json: does not match its SHA-256" in out
    assert out.rstrip().endswith("FAILED changed.zip")


def test_unreadable_inputs_exit_2(tmp_path: Path) -> None:
    assert verify(str(tmp_path / "missing.zip"))[0] == 2
    path = write(tmp_path, evidence.build(RUN, MEMBERS).data)
    code, out = verify(str(path), "--key", "not-a-key")
    assert (code, "32 bytes in base64" in out) == (2, True)


def test_evidence_keygen_writes_a_private_key_once(tmp_path: Path) -> None:
    out = tmp_path / "keys" / "evidence.pem"
    result = runner.invoke(app, ["admin", "evidence-keygen", "--out", str(out)])
    assert result.exit_code == 0, result.output
    signer = evidence.EvidenceSigner.from_pem(out.read_bytes())
    assert signer.public_key in result.output
    again = runner.invoke(app, ["admin", "evidence-keygen", "--out", str(out)])
    assert again.exit_code == 2
