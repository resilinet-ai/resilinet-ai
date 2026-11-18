"""Waivers with expiry and the release gate (B-33: FR-12.1, FR-12.2).

An open critical finding blocks the project's release gate. A waiver lifts it while
its latest decision, by a named reviewer other than the requester and the finding's
owner, approves it and it has not expired.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from rai.core.tenancy import set_context
from tests.integration.apikit import needs_db, signed_in_member
from tests.integration.runkit import Lab
from tests.integration.test_api_keys import bearer, new_key
from tests.integration.test_findings import Story, failing, open_on, outcomes

pytestmark = needs_db


@pytest.fixture
def story(lab: Lab, feed: Path, worker_engine: Engine, tmp_path: Path) -> Story:
    return Story(lab, feed, worker_engine, tmp_path)


@contextmanager
def as_app(owner: Engine, lab: Lab) -> Iterator[Connection]:
    """A transaction as the app role in the lab's tenant: an audited write needs one."""
    with owner.begin() as conn:
        set_context(conn, lab.tenant.tenant_id, "user", "test")
        yield conn


def soon(days: float = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def gate(client: TestClient, lab: Lab, headers: dict[str, str] | None = None) -> Any:
    response = client.get(f"/v1/projects/{lab.project}/gate", headers=headers or lab.viewer)
    assert response.status_code == 200, response.text
    return response.json()


def critical(story: Story, client: TestClient, owner: Engine, **extra: Any) -> dict[str, Any]:
    dirty = story.run()
    created = open_on(client, story.lab, dirty, failing(owner, dirty), **extra)
    assert created.status_code == 201, created.text
    finding: dict[str, Any] = created.json()
    return finding


def ask(client: TestClient, finding: dict[str, Any], headers: dict[str, str],
        **body: Any) -> Any:  # fmt: skip
    return client.post(f"/v1/findings/{finding['finding_id']}/waivers",
                       json={"reason": "fix ships next sprint", "expires_at": soon()} | body,
                       headers=headers)  # fmt: skip


def decide(client: TestClient, waiver_id: str, decision: str, headers: dict[str, str]) -> Any:
    return client.post(f"/v1/waivers/{waiver_id}/decisions",
                       json={"decision": decision, "comment": "ok"}, headers=headers)  # fmt: skip


def test_a_critical_finding_blocks_the_gate_until_another_reviewer_waives_it(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    assert gate(client, lab)["verdict"] == "pass"  # nothing found yet
    finding = critical(story, client, owner)  # opened and owned by the engineer
    blocked = gate(client, lab)
    assert (blocked["verdict"], blocked["blocking_count"]) == ("blocked", 1)
    assert blocked["blocking"][0]["finding_id"] == finding["finding_id"]

    asked = ask(client, finding, lab.engineer)
    assert asked.status_code == 201, asked.text
    waiver = asked.json()
    assert (waiver["status"], waiver["in_force"]) == ("pending", False)
    assert asked.headers["Location"] == f"/v1/waivers/{waiver['waiver_id']}"
    again = ask(client, finding, lab.engineer)
    assert (again.status_code, again.json()["code"]) == (409, "waivers.exists")
    assert gate(client, lab)["verdict"] == "blocked"  # pending does not count

    assert decide(client, waiver["waiver_id"], "approve", lab.engineer).status_code == 403  # role
    approved = decide(client, waiver["waiver_id"], "approve", lab.admin)
    assert approved.status_code == 201, approved.text
    body = approved.json()
    assert (body["status"], body["in_force"], len(body["decisions"])) == ("approved", True, 1)
    passed = gate(client, lab)
    assert (passed["verdict"], passed["waived_count"]) == ("pass", 1)
    assert passed["waived"][0]["waiver_id"] == waiver["waiver_id"]

    # A later decision withdraws it: append-only, the latest counts.
    reviewer = signed_in_member(client, owner, lab.tenant, "reviewer")
    assert decide(client, waiver["waiver_id"], "reject", reviewer).json()["in_force"] is False
    assert gate(client, lab)["verdict"] == "blocked"

    with owner.connect() as conn:
        actions = conn.execute(
            text("SELECT action FROM rai.audit_chain WHERE action LIKE 'waiver%' "
                 "AND tenant_id = :t ORDER BY seq"), {"t": lab.tenant.tenant_id},
        ).scalars().all()  # fmt: skip
    assert actions == ["waiver.insert", "waiver_decision.insert", "waiver_decision.insert"]


def test_no_one_decides_a_waiver_they_asked_for_or_on_a_finding_they_own(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    reviewer = signed_in_member(client, owner, lab.tenant, "reviewer")
    me = client.get("/v1/me", headers=reviewer).json()["user_id"]
    finding = critical(story, client, owner, owner_id=me)  # the reviewer owns it
    waiver = ask(client, finding, lab.admin).json()  # the admin asks
    for who in (lab.admin, reviewer):  # the requester, then the owner
        refused = decide(client, waiver["waiver_id"], "approve", who)
        assert (refused.status_code, refused.json()["code"]) == (403, "waivers.self_approval")
    third = signed_in_member(client, owner, lab.tenant, "reviewer")
    assert decide(client, waiver["waiver_id"], "approve", third).status_code == 201

    with pytest.raises(DBAPIError, match="someone other than"), as_app(owner, lab) as conn:
        conn.execute(
            text("INSERT INTO rai.waiver_decisions (tenant_id, waiver_id, reviewer_id, decision) "
                 "SELECT tenant_id, waiver_id, requested_by, 'approve' FROM rai.waivers "
                 "WHERE waiver_id = :w"), {"w": waiver["waiver_id"]},
        )  # fmt: skip


def test_a_waiver_has_a_bounded_expiry_and_stops_counting_when_it_passes(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    finding = critical(story, client, owner)
    for expires_at, code in [
        (soon(91), "waivers.expiry_out_of_range"),
        (soon(-1), "waivers.expiry_out_of_range"),
        ("2026-12-01T00:00:00", "waivers.expiry_without_zone"),
    ]:
        refused = ask(client, finding, lab.engineer, expires_at=expires_at)
        assert (refused.status_code, refused.json()["code"]) == (422, code), expires_at

    # A waiver approved last week that expired yesterday, written straight to the database.
    with as_app(owner, lab) as conn:
        waiver_id = conn.execute(
            text("INSERT INTO rai.waivers (tenant_id, finding_id, reason, expires_at, "
                 "requested_by, created_at) SELECT tenant_id, finding_id, 'old', "
                 "now() - interval '1 day', created_by, now() - interval '10 days' "
                 "FROM rai.findings WHERE finding_id = :f RETURNING waiver_id"),
            {"f": finding["finding_id"]},
        ).scalar_one()  # fmt: skip
        conn.execute(
            text("INSERT INTO rai.waiver_decisions (tenant_id, waiver_id, reviewer_id, decision, "
                 "created_at) SELECT tenant_id, :w, :admin, 'approve', now() - interval '5 days' "
                 "FROM rai.waivers WHERE waiver_id = :w"),
            {"w": waiver_id, "admin": lab.tenant.admin_id},
        )  # fmt: skip
    old = client.get(f"/v1/waivers/{waiver_id}", headers=lab.viewer).json()
    assert (old["status"], old["expired"], old["in_force"]) == ("approved", True, False)
    assert gate(client, lab)["verdict"] == "blocked"  # it lapsed with no one acting
    late = decide(client, str(waiver_id), "approve", lab.admin)
    assert (late.status_code, late.json()["code"]) == (409, "waivers.expired")
    assert ask(client, finding, lab.engineer).status_code == 201  # a new one may be asked for
    listed = client.get(f"/v1/findings/{finding['finding_id']}/waivers", headers=lab.viewer)
    assert listed.json()[0]["waiver_id"] == str(waiver_id)


def test_only_open_critical_findings_block_and_can_be_waived(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    dirty = story.run()
    keys = sorted(k for k, v in outcomes(owner, dirty).items() if v == "fail")
    high = open_on(client, lab, dirty, keys[0], severity="high").json()
    assert gate(client, lab)["verdict"] == "pass"  # a high finding does not block
    refused = ask(client, high, lab.engineer)
    assert (refused.status_code, refused.json()["code"]) == (422, "waivers.not_blocking")
    if len(keys) > 1:
        crit = open_on(client, lab, dirty, keys[1]).json()
        client.post(f"/v1/findings/{crit['finding_id']}/dismiss", json={"reason": "expected"},
                    headers=lab.admin)  # fmt: skip
        assert gate(client, lab)["verdict"] == "pass"  # a dismissed finding does not block
        gone = ask(client, crit, lab.engineer)
        assert (gone.status_code, gone.json()["code"]) == (409, "findings.resolved")


def test_the_gate_is_read_with_a_project_key_and_hidden_from_others(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    critical(story, client, owner)
    key = bearer(new_key(lab, scopes=["runs:read"])["key"])
    assert gate(client, lab, key)["verdict"] == "blocked"
    starter = bearer(new_key(lab, scopes=["runs:start"])["key"])
    assert client.get(f"/v1/projects/{lab.project}/gate", headers=starter).status_code == 403
    other = lab.post("/v1/projects", {"name": "other"})["project_id"]
    assert client.get(f"/v1/projects/{other}/gate", headers=key).status_code == 404
    stranger = Lab(client, owner)
    assert client.get(f"/v1/projects/{lab.project}/gate",
                      headers=stranger.viewer).status_code == 404  # fmt: skip


def test_the_database_keeps_waivers_append_only(
    story: Story, client: TestClient, owner: Engine
) -> None:
    lab = story.lab
    high = critical(story, client, owner, severity="high")
    finding = critical(story, client, owner)
    waiver = ask(client, finding, lab.engineer).json()
    params = {"w": waiver["waiver_id"], "high": high["finding_id"], "f": finding["finding_id"]}
    for statement in (
        "UPDATE rai.waivers SET expires_at = expires_at + interval '1 year' WHERE waiver_id = :w",
        "DELETE FROM rai.waivers WHERE waiver_id = :w",
    ):
        with pytest.raises(DBAPIError, match="insert-only"), owner.begin() as conn:
            conn.execute(text(statement), params)
    inserts = [
        ("INSERT INTO rai.waivers (tenant_id, finding_id, reason, expires_at, requested_by) "
         "SELECT tenant_id, :high, 'x', now() + interval '1 day', created_by FROM rai.findings "
         "WHERE finding_id = :high", "open critical finding"),
        ("INSERT INTO rai.waivers (tenant_id, finding_id, reason, expires_at, requested_by) "
         "SELECT tenant_id, finding_id, 'x', now() + interval '1 year', created_by "
         "FROM rai.findings WHERE finding_id = :f", "check constraint"),
    ]  # fmt: skip
    for statement, message in inserts:
        with pytest.raises(DBAPIError, match=message), as_app(owner, lab) as conn:
            conn.execute(text(statement), params)
