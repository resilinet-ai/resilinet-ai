"""Release decisions (B-42: FR-12.1, FR-12.2).

A named reviewer approves or rejects a labelled release. Approval is refused while the
gate is blocked and from anyone the release's waivers concern; every decision records
the gate as it stood, computed by the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tests.integration.apikit import needs_db, signed_in_member
from tests.integration.runkit import Lab
from tests.integration.test_api_keys import bearer, new_key
from tests.integration.test_findings import Story
from tests.integration.test_waivers import as_app, ask, critical, decide

pytestmark = needs_db


@pytest.fixture
def story(lab: Lab, feed: Path, worker_engine: Engine, tmp_path: Path) -> Story:
    return Story(lab, feed, worker_engine, tmp_path)


def release(client: TestClient, lab: Lab, label: str, decision: str, headers: dict[str, str],
            why: str = "all checks reviewed") -> Any:  # fmt: skip
    body = {"decision": decision, "justification": why}
    url = f"/v1/projects/{lab.project}/releases/{label}/decisions"
    return client.post(url, json=body, headers=headers)


def test_a_release_is_approved_only_while_its_gate_passes(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    reviewer = signed_in_member(client, owner, lab.tenant, "reviewer")
    first = release(client, lab, "v1.3", "approve", reviewer)
    assert first.status_code == 201, first.text
    body = first.json()
    assert first.headers["Location"] == f"/v1/projects/{lab.project}/releases/v1.3"
    assert (body["status"], body["latest"]["gate"]["verdict"]) == ("approved", "pass")

    finding = critical(story, client, owner)
    refused = release(client, lab, "v1.4", "approve", reviewer)
    assert (refused.status_code, refused.json()["code"]) == (409, "releases.gate_blocked")
    rejected = release(client, lab, "v1.4", "reject", reviewer, "duplicate txn_id open")
    assert rejected.status_code == 201, rejected.text
    gate = rejected.json()["latest"]["gate"]
    assert (gate["verdict"], gate["blocking_findings"]) == ("blocked", [finding["finding_id"]])

    # A waiver lifts the gate; the decision then records which waiver it relied on.
    waiver = ask(client, finding, lab.engineer).json()
    assert decide(client, waiver["waiver_id"], "approve", lab.admin).status_code == 201
    approved = release(client, lab, "v1.4", "approve", reviewer, "F waived until the fix")
    assert approved.status_code == 201, approved.text
    v14 = approved.json()
    assert v14["status"] == "approved"
    assert [d["decision"] for d in v14["decisions"]] == ["reject", "approve"]  # history kept
    assert v14["latest"]["gate"] == v14["latest"]["gate"] | {
        "verdict": "pass", "blocking_findings": [],
        "waived_findings": [finding["finding_id"]], "waivers": [waiver["waiver_id"]],
    }  # fmt: skip

    listed = client.get(f"/v1/projects/{lab.project}/releases", headers=lab.viewer).json()
    assert [(r["release_label"], r["status"]) for r in listed] == [
        ("v1.4", "approved"), ("v1.3", "approved"),
    ]  # fmt: skip
    with owner.connect() as conn:
        actions = conn.execute(
            text("SELECT action FROM rai.audit_chain WHERE action LIKE 'release_decision%' "
                 "AND tenant_id = :t"), {"t": lab.tenant.tenant_id},
        ).scalars().all()  # fmt: skip
    assert actions == ["release_decision.insert"] * 3


def test_no_one_approves_a_release_that_relies_on_their_own_waiver_or_finding(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    finder = signed_in_member(client, owner, lab.tenant, "reviewer")
    me = client.get("/v1/me", headers=finder).json()["user_id"]
    finding = critical(story, client, owner, owner_id=me)  # the reviewer owns it
    waiver = ask(client, finding, lab.admin).json()  # the admin asks
    third = signed_in_member(client, owner, lab.tenant, "reviewer")
    assert decide(client, waiver["waiver_id"], "approve", third).status_code == 201

    for who in (lab.admin, finder):  # the waiver's requester, then the finding's owner
        refused = release(client, lab, "v2.0", "approve", who)
        assert (refused.status_code, refused.json()["code"]) == (403, "releases.self_approval")
    assert release(client, lab, "v2.0", "reject", lab.admin).status_code == 201  # may still say no
    assert release(client, lab, "v2.0", "approve", third).status_code == 201


def test_the_database_records_the_gate_itself_and_never_changes_a_decision(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    finding = critical(story, client, owner)
    insert = (
        "INSERT INTO rai.release_decisions (tenant_id, project_id, release_label, decision, "
        "justification, reviewer_id, gate_verdict, blocking_findings, waived_findings, waivers) "
        "VALUES (:t, :p, 'v9', CAST(:d AS rai.approval_decision), 'forged', :u, 'pass', "
        "'{}', '{}', '{}') RETURNING gate_verdict, blocking_findings"
    )
    args = {"t": lab.tenant.tenant_id, "p": lab.project, "u": lab.tenant.admin_id}
    with pytest.raises(DBAPIError, match="gate is blocked"), as_app(owner, lab) as conn:
        conn.execute(text(insert), args | {"d": "approve"})
    with as_app(owner, lab) as conn:
        verdict, blocking = conn.execute(text(insert), args | {"d": "reject"}).one()
    assert (verdict, [str(b) for b in blocking]) == ("blocked", [finding["finding_id"]])
    for change in ("UPDATE rai.release_decisions SET decision = 'approve'",
                   "DELETE FROM rai.release_decisions"):  # fmt: skip
        with pytest.raises(DBAPIError), as_app(owner, lab) as conn:
            conn.execute(text(change))


def test_who_may_decide_and_read_releases(story: Story, client: TestClient, owner: Engine) -> None:
    lab = story.lab
    for who in (lab.engineer, lab.viewer):
        refused = release(client, lab, "v1.0", "approve", who)
        assert (refused.status_code, refused.json()["code"]) == (403, "auth.forbidden")
    blank = release(client, lab, "v1.0", "approve", lab.admin, why="")
    assert blank.status_code == 422  # a justification is required
    bad = release(client, lab, "../v1", "approve", lab.admin)
    assert bad.status_code in (404, 422)  # not a label
    missing = client.get(f"/v1/projects/{lab.project}/releases/v1.0", headers=lab.viewer)
    assert (missing.status_code, missing.json()["code"]) == (404, "releases.not_found")

    assert release(client, lab, "v1.0", "approve", lab.admin).status_code == 201
    key = bearer(new_key(lab, scopes=["runs:read"])["key"])
    read = client.get(f"/v1/projects/{lab.project}/releases/v1.0", headers=key)
    assert (read.status_code, read.json()["status"]) == (200, "approved")
    by_key = release(client, lab, "v1.1", "approve", key)
    assert (by_key.status_code, by_key.json()["code"]) == (403, "auth.key_scope")
