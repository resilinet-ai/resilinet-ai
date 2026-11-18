"""Evidence export of a finished run (B-34: FR-13.1; B-35: FR-13.2; threats T-02, T-04, I-04)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, make_url, text
from typer.testing import CliRunner

from rai.api.app import create_app
from rai.api.settings import Settings
from rai.cli.main import app as cli
from rai.core import evidence
from rai.worker.service import Worker
from tests.integration.apikit import DSN, SECRET, needs_db
from tests.integration.runkit import Lab, config, drain
from tests.integration.test_api_keys import bearer, new_key
from tests.integration.test_scenario_recovery import System, run, setup
from tests.integration.test_scenarios import Chaos, chaos

pytestmark = needs_db
__all__ = ["chaos", "setup"]  # fixtures shared with the scenario tests


def url(run_id: str) -> str:
    return f"/v1/assurance/runs/{run_id}/evidence"


def finished(lab: Lab, worker_engine: Engine, feed: Path, tmp: Path, *tests: str) -> str:
    run_id: str = lab.start(lab.plan(*tests))["run_id"]
    drain(Worker(worker_engine, config(feed, tmp)))
    return run_id


def export(client: TestClient, run_id: str, headers: dict[str, str]) -> Any:
    response = client.get(url(run_id), headers=headers)
    assert response.status_code == 200, response.text
    return response


def contents(data: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {n.split("/", 1)[1]: json.loads(archive.read(n)) for n in archive.namelist()}


def test_a_finished_run_exports_the_same_bytes_every_time(
    lab: Lab, client: TestClient, owner: Engine, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    tests = (lab.tests["batch_0"], lab.tests["batch_1"])
    run_id = finished(lab, worker_engine, feed, tmp_path, *tests)
    first = export(client, run_id, lab.viewer)
    assert first.headers["content-type"] == "application/zip"
    assert first.headers["content-disposition"] == (
        f'attachment; filename="rai-evidence-{run_id}.zip"')  # fmt: skip
    check = evidence.verify(first.content)
    assert check.ok, check.problems
    assert first.headers["x-rai-manifest-sha256"] == check.manifest_sha256

    finished(lab, worker_engine, feed, tmp_path / "later", lab.tests["batch_0"])  # chain moves on
    second = export(client, run_id, lab.engineer)
    assert second.content == first.content  # FR-13.1: deterministic

    files = contents(first.content)
    assert set(files) == {"bundle.json", "run.json", "steps.json", "observations.json",
                          "lineage.json", "recovery.json", "approvals.json", "audit.json",
                          "manifest.json"}  # fmt: skip
    assert files["run.json"]["state"] == "failed"  # batch 1 re-sends rows of batch 0
    assert files["run.json"]["pins"]["format"] == "rai-run-pins/1"
    assert [s["step_key"] for s in files["steps.json"]] == ["item-1", "item-2"]
    with owner.connect() as conn:
        observed = conn.execute(text("SELECT count(*) FROM rai.observations WHERE run_id = :r"),
                                {"r": run_id}).scalar_one()  # fmt: skip
        last = conn.execute(
            text("SELECT seq, record_hash FROM rai.audit_chain WHERE subject_type = 'run' "
                 "AND subject_id = :r ORDER BY seq DESC LIMIT 1"), {"r": run_id},
        ).one()  # fmt: skip
        exports = conn.execute(
            text("SELECT actor_type::text, payload FROM rai.audit_chain "
                 "WHERE action = 'evidence.export' AND subject_id = :r ORDER BY seq"),
            {"r": run_id},
        ).all()  # fmt: skip
    counts = files["bundle.json"]["counts"]
    assert len(files["observations.json"]) == observed == counts["observations"]
    assert all(e["from_digest"] for e in files["lineage.json"] if e["from_name"] != "accounts")
    anchor = files["bundle.json"]["anchor"]
    assert (anchor["seq"], anchor["record_hash"]) == (last.seq, last.record_hash)  # T-02
    assert files["audit.json"][-1]["record_hash"] == last.record_hash
    assert [a for a, _ in exports] == ["user", "user"]  # each export is audited
    assert exports[0].payload["manifest_sha256"] == check.manifest_sha256


def test_only_a_finished_run_of_your_own_project_exports(
    lab: Lab, client: TestClient, owner: Engine, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    queued: str = lab.start(lab.plan(lab.tests["batch_0"]))["run_id"]
    early = client.get(url(queued), headers=lab.viewer)
    assert (early.status_code, early.json()["code"]) == (409, "evidence.run_unfinished")
    drain(Worker(worker_engine, config(feed, tmp_path)))

    key = new_key(lab, scopes=["runs:read"])["key"]
    reader = bearer(key)
    by_key = export(client, queued, reader)
    assert evidence.verify(by_key.content).ok
    starter = bearer(new_key(lab, scopes=["runs:start"])["key"])
    assert client.get(url(queued), headers=starter).status_code == 403
    stranger = Lab(client, owner)
    assert client.get(url(queued), headers=stranger.viewer).status_code == 404
    other = bearer(new_key(stranger, scopes=["runs:read"])["key"])
    assert client.get(url(queued), headers=other).status_code == 404

    # I-04: neither the key that exported it nor the database password is in the bundle.
    password = make_url(DSN).password
    assert password
    assert key.encode() not in by_key.content
    assert password.encode() not in by_key.content


def test_a_chaos_run_carries_its_approval_and_recovery_measurement(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path,
    client: TestClient,
) -> None:  # fmt: skip
    chaos, system = setup
    system.heads.append({"seq": 104, "committed_at": "2026-11-17T12:00:30Z", "rows": 104})
    done, _ = run(chaos, worker_engine, feed, tmp_path, rto_seconds=30, rpo_seconds=0)
    files = contents(export(client, done["run_id"], chaos.lab.viewer).content)
    [pinned] = [i["scenario"] for i in files["run.json"]["pins"]["items"] if i.get("scenario")]
    [approval] = files["approvals.json"]
    assert (approval["approval_id"], approval["decision"]) == (pinned["approval_id"], "approve")
    [measurement] = files["recovery.json"]
    assert (measurement["step_key"], measurement["rto_met"]) == ("item-1", True)


def test_a_signing_server_signs_bundles_that_rai_verify_trusts(
    api_url: str, owner: Engine, worker_engine: Engine, feed: Path, tmp_path: Path
) -> None:
    """FR-13.2: a signed manifest, checked offline against a pinned public key."""
    signer = evidence.EvidenceSigner.generate()
    settings = Settings(database_url=api_url, jwt_secret=SECRET, evidence_signer=signer)
    with TestClient(create_app(settings)) as signing:
        lab = Lab(signing, owner)
        run_id = finished(lab, worker_engine, feed, tmp_path, lab.tests["batch_0"])
        first, second = export(signing, run_id, lab.viewer), export(signing, run_id, lab.viewer)
        published = signing.get("/v1/evidence/public-key", headers=lab.viewer).json()
    assert first.content == second.content  # still deterministic
    assert first.headers["x-rai-signature-key-id"] == signer.key_id
    assert published == {"algorithm": "ed25519", "key_id": signer.key_id,
                         "public_key": signer.public_key}  # fmt: skip
    note = contents(first.content)["bundle.json"]["signature"]
    assert note == {"algorithm": "ed25519", "key_id": signer.key_id, "member": "signature.json"}

    path = tmp_path / "bundle.zip"
    path.write_bytes(first.content)
    result = CliRunner().invoke(cli, ["verify", str(path), "--key", signer.public_key])
    assert result.exit_code == 0, result.output
    with owner.connect() as conn:
        payload = conn.execute(
            text("SELECT payload FROM rai.audit_chain WHERE action = 'evidence.export' "
                 "AND subject_id = :r ORDER BY seq LIMIT 1"), {"r": run_id},
        ).scalar_one()  # fmt: skip
    assert payload["key_id"] == signer.key_id


def test_a_server_without_a_key_exports_unsigned_bundles(lab: Lab, client: TestClient) -> None:
    missing = client.get("/v1/evidence/public-key", headers=lab.viewer)
    assert (missing.status_code, missing.json()["code"]) == (404, "evidence.unsigned")
