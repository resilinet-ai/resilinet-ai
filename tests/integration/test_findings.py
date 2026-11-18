"""Findings: severity, owner, fix and retest (FR-10.1).

A finding is opened on a failing observation of a run. A retest closes it only when a
later run of the project passes the same rule for the same test; to get one, the
test's batch file is replaced with a clean batch between the two runs.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import TXN_YAML, Lab, config, drain

pytestmark = needs_db


class Story:
    """One test case over its own batch file: run it dirty, then clean."""

    def __init__(self, lab: Lab, feed: Path, worker_engine: Engine, tmp: Path) -> None:
        self.lab, self.feed, self.engine, self.tmp = lab, feed, worker_engine, tmp
        self.name = f"story_{uuid4().hex[:6]}"
        self.file = feed / "feed" / "transactions_raw" / f"{self.name}.csv"
        clean = (feed / "feed" / "transactions_raw" / "B000000.csv").read_text("utf-8")
        lines = clean.splitlines(keepends=True)
        self.file.write_text(clean + lines[1], "utf-8")  # a duplicated row: txn_id not unique
        asset = lab.post(f"/v1/projects/{lab.project}/assets",
                         {"kind": "table", "name": self.name,
                          "locator": f"transactions_raw/{self.name}.csv"})  # fmt: skip
        lab.post(f"/v1/assets/{asset['asset_id']}/contracts", {"source": TXN_YAML})
        self.test_id = lab.test_case(self.name, asset["asset_id"])
        self.plan = lab.plan(self.test_id)
        self.runs = 0

    def run(self) -> str:
        """Start and finish a run; each with its own worker state (no shared history)."""
        self.runs += 1
        run_id: str = self.lab.start(self.plan)["run_id"]
        drain(Worker(self.engine, config(self.feed, self.tmp / f"w{self.runs}")))
        return run_id

    def clean(self) -> None:
        shutil.copy(self.feed / "feed" / "transactions_raw" / "B000000.csv", self.file)


def outcomes(owner: Engine, run_id: str) -> dict[tuple[str, str], str]:
    with owner.connect() as conn:
        rows = conn.execute(
            text("SELECT step_key, rule, outcome::text FROM rai.observations WHERE run_id = :r"),
            {"r": run_id},
        ).all()
    return {(r.step_key, r.rule): r.outcome for r in rows}


@pytest.fixture
def story(lab: Lab, feed: Path, worker_engine: Engine, tmp_path: Path) -> Story:
    return Story(lab, feed, worker_engine, tmp_path)


def open_on(client: TestClient, lab: Lab, run_id: str, key: tuple[str, str],
            headers: dict[str, str] | None = None, **extra: Any) -> Any:  # fmt: skip
    body = {"run_id": run_id, "step_key": key[0], "rule": key[1], "title": "Bad rows",
            "severity": "critical"} | extra  # fmt: skip
    return client.post(f"/v1/projects/{lab.project}/findings", json=body,
                       headers=headers or lab.engineer)  # fmt: skip


def failing(owner: Engine, run_id: str) -> tuple[str, str]:
    return sorted(k for k, v in outcomes(owner, run_id).items() if v == "fail")[0]


def test_a_finding_is_opened_on_a_failing_observation_once(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    dirty = story.run()
    key = failing(owner, dirty)
    created = open_on(client, lab, dirty, key, description="seeded defects")
    assert created.status_code == 201, created.text
    finding = created.json()
    assert (finding["status"], finding["severity"]) == ("open", "critical")
    assert finding["owner_id"] == finding["created_by"]  # default owner: whoever opened it
    assert created.headers["Location"] == f"/v1/findings/{finding['finding_id']}"

    again = open_on(client, lab, dirty, key)
    assert (again.status_code, again.json()["code"]) == (409, "findings.duplicate")
    assert again.headers["Location"] == created.headers["Location"]

    passed = sorted(k for k, v in outcomes(owner, dirty).items() if v == "pass")[0]
    refused = open_on(client, lab, dirty, passed)
    assert (refused.status_code, refused.json()["code"]) == (422, "findings.source_not_failing")
    unknown = open_on(client, lab, dirty, ("item-9", "nothing"))
    assert unknown.json()["code"] == "findings.source_not_found"
    assert open_on(client, lab, dirty, key, lab.viewer).status_code == 403

    listed = client.get(f"/v1/projects/{lab.project}/findings", params={"status": "open"},
                        headers=lab.viewer).json()  # fmt: skip
    assert finding["finding_id"] in [f["finding_id"] for f in listed["items"]]
    with owner.connect() as conn:
        actions = conn.execute(
            text("SELECT action FROM rai.audit_chain WHERE subject_id = :f"),
            {"f": finding["finding_id"]},
        ).scalars().all()  # fmt: skip
    assert actions == ["finding.insert"]


def test_only_a_later_passing_retest_closes_a_finding(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    dirty = story.run()
    key = failing(owner, dirty)
    finding = open_on(client, lab, dirty, key).json()
    url = f"/v1/findings/{finding['finding_id']}"

    def retest(run_id: str) -> Any:
        return client.post(f"{url}/retest", json={"run_id": run_id}, headers=lab.engineer)

    assert retest(dirty).json()["code"] == "findings.retest_too_early"  # its own source run
    queued = lab.start(story.plan)["run_id"]  # not processed yet
    assert retest(queued).json()["code"] == "findings.retest_unfinished"
    drain(Worker(story.engine, config(story.feed, story.tmp / "drain")))  # still dirty data
    assert outcomes(owner, queued)[key] == "fail"
    assert retest(queued).json()["code"] == "findings.retest_not_passing"
    assert client.get(url, headers=lab.viewer).json()["status"] == "open"

    fixed = client.patch(url, json={"fix_ref": "https://example.org/pull/41"},
                         headers=lab.engineer)  # fmt: skip
    assert fixed.json()["fix_ref"] == "https://example.org/pull/41"
    story.clean()
    clean = story.run()
    assert outcomes(owner, clean)[key] == "pass"
    closed = retest(clean)
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert (body["status"], body["retest_run_id"]) == ("closed", clean)
    assert body["resolved_at"] is not None

    late = client.patch(url, json={"fix_ref": "other"}, headers=lab.engineer)
    assert (late.status_code, late.json()["code"]) == (409, "findings.resolved")


def test_dismissal_needs_a_reason_and_another_person(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    dirty = story.run()
    rules = sorted(k for k, v in outcomes(owner, dirty).items() if v == "fail")
    by_engineer = open_on(client, lab, dirty, rules[0]).json()
    url = f"/v1/findings/{by_engineer['finding_id']}/dismiss"
    assert client.post(url, json={"reason": "x"}, headers=lab.engineer).status_code == 403
    assert client.post(url, json={}, headers=lab.admin).status_code == 422  # a reason
    done = client.post(url, json={"reason": "expected: seeded defect"}, headers=lab.admin)
    assert (done.status_code, done.json()["status"]) == (200, "dismissed")

    if len(rules) > 1:  # an admin cannot dismiss a finding they opened
        own = open_on(client, lab, dirty, rules[1], lab.admin).json()
        mine = client.post(f"/v1/findings/{own['finding_id']}/dismiss",
                           json={"reason": "x"}, headers=lab.admin)  # fmt: skip
        assert (mine.status_code, mine.json()["code"]) == (403, "findings.self_dismissal")


def test_owner_must_be_a_member_and_other_tenants_see_nothing(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    dirty = story.run()
    finding = open_on(client, lab, dirty, failing(owner, dirty)).json()
    url = f"/v1/findings/{finding['finding_id']}"
    stranger = Lab(client, owner)
    alien = {"owner_id": str(stranger.tenant.admin_id)}
    moved = client.patch(url, json=alien, headers=lab.engineer)
    assert (moved.status_code, moved.json()["code"]) == (422, "findings.owner_not_member")
    assert client.patch(url, json={"owner_id": None}, headers=lab.engineer).status_code == 422
    assert client.get(url, headers=stranger.viewer).status_code == 404


def test_the_database_guards_findings(story: Story, client: TestClient, owner: Engine) -> None:
    lab = story.lab
    dirty = story.run()
    key = failing(owner, dirty)
    finding = open_on(client, lab, dirty, key).json()
    later = story.run()  # still failing: not a passing retest
    statements = [
        ("UPDATE rai.findings SET severity = 'low' WHERE finding_id = :f", "never change"),
        ("UPDATE rai.findings SET status = 'closed', retest_run_id = :later, resolved_by = "
         "created_by, resolved_at = now() WHERE finding_id = :f", "passes the rule"),
        ("DELETE FROM rai.findings WHERE finding_id = :f", "insert-only"),
    ]  # fmt: skip
    for statement, message in statements:
        with pytest.raises(DBAPIError, match=message), owner.begin() as conn:
            conn.execute(text(statement), {"f": finding["finding_id"], "later": later})
