"""The five user journeys of Spec section 13, end to end (B-46: NFR-REP-2).

Each test is one journey, in the order a team meets them, on one live stack: the API
and a worker as processes, SandboxBank and the chaos agent in Docker. A journey
builds on the ones before it (J4 decides on J2's failure, J5 exports J2's and J3's
runs), so they share a :class:`World` and a later one is skipped, not failed, when
an earlier one did not get far enough. Synthetic data only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.stack import (
    AGENT,
    CONTRACT,
    ENVIRONMENT,
    PAYMENTS,
    Person,
    Stack,
    docker_image,
    eventually,
)

REFERENCES = {"accounts": "accounts.csv", "merchants": "merchants.csv"}
DROP_SECONDS = 5


@dataclass
class World:
    people: dict[str, Person] = field(default_factory=dict)
    environment_id: str = ""
    project_id: str = ""
    api_key: str = ""
    runs: dict[str, str] = field(default_factory=dict)

    def need(self, *what: str) -> None:
        missing = [w for w in what if not (getattr(self, w, None) or w in self.runs)]
        if missing:
            pytest.skip(f"an earlier journey did not provide {', '.join(missing)}")


@pytest.fixture(scope="module")
def world() -> World:
    return World()


# ---- helpers --------------------------------------------------------------------------------


def contract_rule_test(engineer: Person, project_id: str, name: str, locator: str) -> str:
    asset = engineer.post(
        f"/v1/projects/{project_id}/assets", {"kind": "table", "name": name, "locator": locator}
    )
    engineer.post(
        f"/v1/assets/{asset['asset_id']}/contracts", {"source": CONTRACT.read_text("utf-8")}
    )
    body = {
        "name": f"contract.{name}",
        "type": "contract_rule",
        "target_asset_id": asset["asset_id"],
        "definition": {"connector": "feed", "environment": ENVIRONMENT, "references": REFERENCES},
    }
    test_id: str = engineer.post(f"/v1/projects/{project_id}/test-cases", body)["test_id"]
    return test_id


def plan(
    engineer: Person, project_id: str, name: str, items: list[dict[str, Any]], environment_id: str
) -> str:
    body = {"name": name, "items": items, "environment_id": environment_id}
    plan_id: str = engineer.post(f"/v1/projects/{project_id}/plans", body)["plan_id"]
    return plan_id


def rai_run(
    stack: Stack, api_key: str, plan_name: str, tmp: Path
) -> tuple[int, dict[str, str], str]:
    """``rai run`` exactly as the GitHub Action calls it; returns its exit code and outputs."""
    outputs = tmp / f"{plan_name}-{os.urandom(3).hex()}.out"
    done = stack.cli(
        "rai",
        "run",
        "--plan",
        plan_name,
        "--timeout",
        "600",
        "--interval",
        "1",
        check=False,
        RAI_API_URL=stack.api_url,
        RAI_API_KEY=api_key,
        GITHUB_OUTPUT=str(outputs),
        GITHUB_SHA="0" * 40,
    )
    pairs = dict(line.split("=", 1) for line in outputs.read_text("utf-8").splitlines())
    return done.returncode, pairs, done.stdout + done.stderr


def observations(person: Person, run_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    path = f"/v1/assurance/runs/{run_id}/observations?limit=200"
    page = person.get(path)
    items += page["items"]
    while page["next_cursor"]:
        page = person.get(f"{path}&cursor={page['next_cursor']}")
        items += page["items"]
    return items


def bundle_members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}


def export(person: Person, run_id: str, out: Path) -> Path:
    response = person.http.get(f"/v1/assurance/runs/{run_id}/evidence")
    assert response.status_code == 200, response.text
    out.write_bytes(response.content)
    return out


def outcomes(person: Person, run_id: str) -> set[tuple[str, str, str, str]]:
    """What a run found, without anything that names the run itself."""
    return {
        (o["step_key"], o["rule"], o["outcome"], json.dumps(o["observed_value"], sort_keys=True))
        for o in observations(person, run_id)
    }


def files(members: dict[str, bytes]) -> list[str]:
    return [f["path"] for f in json.loads(members["manifest.json"])["files"]]


def steps(members: dict[str, bytes]) -> list[tuple[str, str, str | None]]:
    return [(s["step_key"], s["state"], s["verdict"]) for s in json.loads(members["steps.json"])]


# ---- J1 -------------------------------------------------------------------------------------


def test_j1_onboard_a_sandbox(stack: Stack, world: World) -> None:
    """Admin: invite members with roles; register SANDBOX-01 and its allow-list; configure a
    connector with a secret reference; create a project classified synthetic. Every step
    is in the audit chain."""
    admin = stack.sign_in(stack.admin_email, "admin")
    world.people["admin"] = admin
    for key, role in (
        ("engineer", "engineer"),
        ("reviewer", "reviewer"),
        ("reviewer2", "reviewer"),
        ("viewer", "viewer"),
    ):
        email = f"{key}-{os.urandom(3).hex()}@example.test"
        admin.post("/v1/members", {"email": email, "display_name": key.title(), "role": role})
        stack.activate(email)  # the operator sets the first password
        world.people[key] = stack.sign_in(email, role)
    roles = {m["email"]: m["role"] for m in admin.get("/v1/members")}
    assert {roles[p.email] for p in world.people.values()} == {
        "admin",
        "engineer",
        "reviewer",
        "viewer",
    }

    engineer = world.people["engineer"]
    refused = engineer.post(
        "/v1/environments", {"name": ENVIRONMENT, "type": "SANDBOX"}, expect=403
    )
    assert refused.get("code"), refused
    environment = admin.post("/v1/environments", {"name": ENVIRONMENT, "type": "SANDBOX"})
    world.environment_id = environment["environment_id"]
    targets = f"/v1/environments/{world.environment_id}/targets"
    image = docker_image("rai.target=payments-api")
    for target in (
        {"kind": "proxy", "name": "ledger-db-proxy", "required_label": "rai.target=toxiproxy"},
        {
            "kind": "container",
            "name": "payments-api",
            "required_label": "rai.target=payments-api",
            "image_digest": image,
        },
        {"kind": "http", "name": "payments-health", "health_url": f"{PAYMENTS}/readyz"},
        {"kind": "http", "name": "payments-ledger", "health_url": f"{PAYMENTS}/v1/ledger/head"},
    ):
        admin.post(targets, target)
    unpinned = admin.post(
        targets,
        {"kind": "container", "name": "etl-job", "required_label": "rai.target=etl-job"},
        expect=422,
    )
    assert unpinned["code"] == "targets.image_required"
    listed = {t["name"]: t for t in admin.get(targets)["items"]}
    assert listed["payments-api"]["image_digest"] == image

    # The connectors hold references, never credentials (FR-02.3); J2 and J3 prove they work.
    configs = {p.name: p.read_text("utf-8") for p in stack.connectors.glob("*.yaml")}
    assert "secret_ref: env:LEDGER_DB_PASSWORD" in configs["ledger.yaml"]
    assert "secret_ref: env:ANALYTICS_DB_PASSWORD" in configs["warehouse.yaml"]
    secrets = [os.environ["LEDGER_DB_PASSWORD"], os.environ["ANALYTICS_DB_PASSWORD"]]
    assert not any(s in text for s in secrets for text in configs.values())

    project = engineer.post(
        "/v1/projects",
        {
            "name": "SandboxBank payments",
            "data_classification": "synthetic",
            "default_environment_id": world.environment_id,
        },
    )
    assert project["data_classification"] == "synthetic"
    world.project_id = project["project_id"]

    chain = stack.audit()
    actions = [entry["action"] for entry in chain]
    assert sum(a.startswith("member") for a in actions) >= 4, actions
    assert any(a.startswith("environment") for a in actions), actions
    assert sum("target" in a for a in actions) == 4, actions  # the refused one is not there
    assert any(a.startswith("project") for a in actions), actions
    verified = stack.cli(
        "rai", "audit", "verify", "--tenant", stack.tenant_id, RAI_DATABASE_URL=stack.owner_url
    )
    assert "audit chain OK" in verified.stdout


# ---- J2 -------------------------------------------------------------------------------------


def test_j2_validate_in_ci(stack: Stack, world: World, tmp_path: Path) -> None:
    """Engineer: publish a contract for transactions_raw; CI runs ``rai run``; a critical
    failure blocks the merge. The failing run has observations and lineage."""
    world.need("project_id", "environment_id")
    engineer, viewer = world.people["engineer"], world.people["viewer"]
    tests = {
        name: contract_rule_test(engineer, world.project_id, name, f"transactions_raw/{batch}.csv")
        for name, batch in (("batch_0", "B000000"), ("batch_1", "B000001"))
    }
    items = lambda *names: [{"test_id": tests[n]} for n in names]  # noqa: E731
    plan(engineer, world.project_id, "ci-clean", items("batch_0"), world.environment_id)
    plan(engineer, world.project_id, "ci-resent", items("batch_0", "batch_1"), world.environment_id)
    key = engineer.post("/v1/api-keys", {"project_id": world.project_id, "name": "github-actions"})
    world.api_key = key["key"]

    code, out, log = rai_run(stack, world.api_key, "ci-clean", tmp_path)
    assert (code, out["verdict"], out["gate"]) == (0, "pass", "pass"), log
    world.runs["clean"] = out["run-id"]

    code, out, log = rai_run(stack, world.api_key, "ci-resent", tmp_path)
    assert (code, out["verdict"]) == (1, "fail"), log  # the merge is blocked
    assert "unique_across_batches:txn_id" in log
    world.runs["resent"] = out["run-id"]

    run = viewer.get(f"/v1/assurance/runs/{world.runs['resent']}")
    assert (run["state"], run["verdict"]) == ("failed", "fail")
    assert run["trigger_ref"] == "0" * 40
    failing = [o for o in observations(viewer, world.runs["resent"]) if o["outcome"] == "fail"]
    assert {o["rule"] for o in failing} == {"unique_across_batches:txn_id"}, failing
    assert failing[0]["step_key"] == "item-2"
    lineage = viewer.get(f"/v1/assurance/runs/{world.runs['resent']}/lineage")
    assert {a["name"] for a in lineage["assets"]} >= {"batch_0", "batch_1"}, lineage
    assert {e["from_name"] for e in lineage["edges"]} >= {"batch_1"}, lineage


# ---- J3 -------------------------------------------------------------------------------------


def test_j3_controlled_chaos(stack: Stack, world: World) -> None:
    """Engineer, then reviewer: "ledger DB connection loss" with RTO and RPO objectives and
    a blast radius; another person approves; the run passes both preflights, injects,
    and gets RTO, RPO and consistency verdicts; the fault is removed at its duration."""
    world.need("project_id", "environment_id")
    engineer, reviewer = world.people["engineer"], world.people["reviewer"]
    project = world.project_id

    ledger = engineer.post(
        f"/v1/projects/{project}/assets",
        {"kind": "table", "name": "ledger_transactions", "locator": "ledger_transactions"},
    )
    facts = engineer.post(
        f"/v1/projects/{project}/assets",
        {"kind": "table", "name": "fact_transaction", "locator": "fact_transaction"},
    )
    engineer.post(
        f"/v1/assets/{facts['asset_id']}/inputs",
        {"input_asset_id": ledger["asset_id"], "transform_ref": "etl-job@1"},
    )
    check = engineer.post(
        f"/v1/projects/{project}/test-cases",
        {
            "name": "ledger.consistency",
            "type": "consistency_check",
            "target_asset_id": facts["asset_id"],
            "definition": {
                "connector": "warehouse",
                "environment": ENVIRONMENT,
                "source": {
                    "asset": "ledger_transactions",
                    "connector": "ledger",
                    "where": {"status": "posted"},
                    "rename": {"seq": "ledger_seq"},
                },
                "key": ["txn_id"],
                "compare": ["amount", "ledger_seq"],
                "within_seconds": 240,
                "interval_seconds": 3,
            },
        },
    )["test_id"]
    scenario = engineer.post(
        f"/v1/projects/{project}/scenarios",
        {
            "name": "ledger-db-connection-loss",
            "description": "ledger DB connection loss",
            "blast_radius": ["ledger-db-proxy"],
            "faults": [
                {
                    "type": "connection_drop",
                    "target": "ledger-db-proxy",
                    "duration_seconds": DROP_SECONDS,
                }
            ],
            "recovery": {
                "probes": ["payments-health"],
                "ledger": "payments-ledger",
                "rto_seconds": 60,
                "rpo_seconds": 0,
                "window_seconds": 90,
                "stable_checks": 3,
            },
        },
    )
    assert scenario["status"] == "pending"
    sid = scenario["scenario_id"]
    url = f"/v1/scenarios/{sid}/approvals"
    own = engineer.post(url, {"decision": "approve"}, expect=403)  # separation of duties
    assert own["code"], own
    reviewer.post(url, {"decision": "approve"})
    items = [{"item_type": "scenario", "scenario_id": sid}, {"test_id": check}]
    plan_id = plan(engineer, project, "ledger-outage", items, world.environment_id)

    # Traffic while the fault is on: SandboxBank's seeder posts one write at a time.
    with stack.cli.background(
        "seeder",
        "sandboxbank",
        "seed",
        "--data",
        str(stack.sandbox),
        "--batch",
        "B000001",
        "--log",
        str(stack.work / "acks.csv"),
        "--rate",
        "10",
        "--limit",
        "250",
        "--api",
        PAYMENTS,
    ):
        run_id = engineer.post("/v1/assurance/runs", {"plan_id": plan_id})["run_id"]
        world.runs["chaos"] = run_id
        run = engineer.wait_for_run(run_id, within=600)
    assert (run["state"], run["verdict"]) == ("passed", "pass"), run
    assert run["pins"]["environment"]["images"] == {
        "payments-api": docker_image("rai.target=payments-api")
    }

    seen = {o["rule"]: o for o in observations(engineer, run_id)}
    rto, rpo = seen["recovery.rto"], seen["recovery.rpo"]
    assert rto["outcome"] == "pass", rto
    assert DROP_SECONDS <= rto["observed_value"]["seconds"] < 60, rto
    assert (rpo["outcome"], rpo["observed_value"]["lost_rows"]) == ("pass", 0), rpo
    for finding in ("missing", "duplicated", "unexpected", "mismatched"):
        assert seen[f"consistency.{finding}"]["outcome"] == "pass", seen[f"consistency.{finding}"]

    # The fault is gone: the agent holds nothing open and SandboxBank answers.
    faults = httpx.get(f"{AGENT}/faults", timeout=5).json()["faults"]
    assert faults == [], faults
    eventually(
        lambda: httpx.get(f"{PAYMENTS}/readyz", timeout=5).status_code == 200,
        "payments-api to be ready",
    )
    recovery = engineer.get(f"/v1/assurance/recovery?project_id={project}")
    assert any(r["run_id"] == run_id for r in recovery["items"]), recovery


# ---- J4 -------------------------------------------------------------------------------------


def test_j4_decide_a_release(stack: Stack, world: World, tmp_path: Path) -> None:
    """Reviewer: open the project's gate; see the blocking finding; waive it with an
    expiry; approve the release. The decision is in the audit chain."""
    world.need("project_id", "resent", "api_key")
    engineer, reviewer = world.people["engineer"], world.people["reviewer"]
    approver = world.people["reviewer2"]
    project, resent = world.project_id, world.runs["resent"]
    failing = next(o for o in observations(engineer, resent) if o["outcome"] == "fail")
    finding = engineer.post(
        f"/v1/projects/{project}/findings",
        {
            "run_id": resent,
            "step_key": failing["step_key"],
            "rule": failing["rule"],
            "title": "Batch 2 re-sends transactions",
            "severity": "critical",
        },
    )

    gate = approver.get(f"/v1/projects/{project}/gate")
    assert (gate["verdict"], [f["finding_id"] for f in gate["blocking"]]) == (
        "blocked",
        [finding["finding_id"]],
    )
    decide = f"/v1/projects/{project}/releases/v1.0/decisions"
    blocked = approver.post(
        decide, {"decision": "approve", "justification": "checks reviewed"}, expect=409
    )
    assert blocked["code"] == "releases.gate_blocked"
    code, out, _ = rai_run(stack, world.api_key, "ci-clean", tmp_path)
    assert (code, out["verdict"], out["gate"]) == (1, "pass", "blocked")  # CI sees it too

    expires = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    waiver = engineer.post(
        f"/v1/findings/{finding['finding_id']}/waivers",
        {"reason": "the upstream fix ships next sprint", "expires_at": expires},
    )
    reviewer.post(
        f"/v1/waivers/{waiver['waiver_id']}/decisions",
        {"decision": "approve", "comment": "accepted until the fix ships"},
    )
    assert approver.get(f"/v1/projects/{project}/gate")["verdict"] == "pass"

    release = approver.post(
        decide, {"decision": "approve", "justification": "one critical finding waived to expiry"}
    )
    assert (release["status"], release["latest"]["gate"]["verdict"]) == ("approved", "pass")
    code, out, _ = rai_run(stack, world.api_key, "ci-clean", tmp_path)
    assert (code, out["gate"]) == (0, "pass")

    chain = stack.audit()
    decided = [e for e in chain if e["subject_type"].startswith("release")]
    assert decided, sorted({e["action"] for e in chain})
    assert decided[-1]["actor_id"] == approver.user_id
    verified = stack.cli(
        "rai", "audit", "verify", "--tenant", stack.tenant_id, RAI_DATABASE_URL=stack.owner_url
    )
    assert "audit chain OK" in verified.stdout


# ---- J5 -------------------------------------------------------------------------------------


def test_j5_reproduce_evidence(stack: Stack, world: World, tmp_path: Path) -> None:
    """Reviewer, then an outside researcher: export a run's bundle; the researcher checks
    it offline with ``rai verify`` and re-runs the plan on their own SandboxBank, with
    matching verdicts and manifest."""
    world.need("resent")
    reviewer = world.people["reviewer"]
    original = export(reviewer, world.runs["resent"], tmp_path / "resent.zip")
    again = reviewer.http.get(f"/v1/assurance/runs/{world.runs['resent']}/evidence").content
    assert again == original.read_bytes()  # byte-identical every time
    bundles = [original]
    if world.runs.get("chaos"):
        bundles.append(export(reviewer, world.runs["chaos"], tmp_path / "chaos.zip"))

    # Offline: only the bundle and the public key, which the researcher got out of band.
    for bundle in bundles:
        checked = stack.cli("rai", "verify", str(bundle), "--key", stack.evidence_public_key)
        assert "OK      manifest" in checked.stdout, checked.stdout
        assert "OK      signature" in checked.stdout, checked.stdout
    members = bundle_members(original.read_bytes())
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as archive:
        for name, data in members.items():
            if name == "observations.json":
                data = data.replace(b'"fail"', b'"pass"')
            archive.writestr(f"rai-evidence-x/{name}", data)
    caught = stack.cli(
        "rai", "verify", str(tampered), "--key", stack.evidence_public_key, check=False
    )
    assert caught.returncode == 1, caught.stdout
    assert "FAIL" in caught.stdout
    report = members["report.md"].decode("utf-8")
    assert "## Limitations" in report

    # Their own SandboxBank: the same seeds give the same data, byte for byte.
    from sandboxbank.fixtures import build_slice

    own = tmp_path / "own-slice"
    build_slice(
        own, data_seed=27, injector_seed=28, accounts=200, merchants=40, rows_per_batch=2_000
    )
    for name in ("B000000.csv", "B000001.csv"):
        mine = (own / "feed" / "transactions_raw" / name).read_bytes()
        theirs = (stack.feed / "feed" / "transactions_raw" / name).read_bytes()
        assert hashlib.sha256(mine).digest() == hashlib.sha256(theirs).digest(), name

    # Their own RAI tenant: the plan rebuilt from the bundle's pins, the contract from the
    # repository, checked against the pinned hash.
    run = json.loads(members["run.json"])
    pins = run["pins"]
    lab_admin_email = f"lab-admin-{os.urandom(3).hex()}@example.test"
    tenant = stack.new_tenant(lab_admin_email)
    lab_admin = stack.sign_in(lab_admin_email, "admin", tenant_id=tenant)
    environment = lab_admin.post("/v1/environments", {"name": ENVIRONMENT, "type": "SANDBOX"})
    email = f"researcher-{os.urandom(3).hex()}@example.test"
    lab_admin.post(
        "/v1/members", {"email": email, "display_name": "Researcher", "role": "engineer"}
    )
    stack.activate(email)
    researcher = stack.sign_in(email, "engineer", tenant_id=tenant)
    project = researcher.post(
        "/v1/projects", {"name": "Reproduction", "data_classification": "synthetic"}
    )
    test_ids = []
    for item in pins["items"]:
        asset = researcher.post(
            f"/v1/projects/{project['project_id']}/assets",
            {
                "kind": item["asset"]["kind"] or "table",
                "name": item["asset"]["name"],
                "locator": item["asset"]["locator"],
            },
        )
        contract = researcher.post(
            f"/v1/assets/{asset['asset_id']}/contracts", {"source": CONTRACT.read_text("utf-8")}
        )
        assert contract["spec_sha256"] == item["contract"]["spec_sha256"]
        test = item["test"]
        test_ids.append(
            researcher.post(
                f"/v1/projects/{project['project_id']}/test-cases",
                {
                    "name": test["name"],
                    "type": test["type"],
                    "severity": test["severity"],
                    "target_asset_id": asset["asset_id"],
                    "definition": test["definition"],
                },
            )["test_id"]
        )
    plan(
        researcher,
        project["project_id"],
        pins["plan"]["name"],
        [{"test_id": t} for t in test_ids],
        environment["environment_id"],
    )
    key = researcher.post(
        "/v1/api-keys", {"project_id": project["project_id"], "name": "reproduction"}
    )["key"]
    code, out, log = rai_run(stack, key, pins["plan"]["name"], tmp_path)
    assert (code, out["verdict"]) == (1, run["verdict"]), log

    rerun = export(researcher, out["run-id"], tmp_path / "rerun.zip")
    checked = stack.cli("rai", "verify", str(rerun), "--key", stack.evidence_public_key)
    assert "OK      manifest" in checked.stdout
    reproduced = bundle_members(rerun.read_bytes())
    assert files(reproduced) == files(members)
    assert steps(reproduced) == steps(members)
    assert outcomes(researcher, out["run-id"]) == outcomes(reviewer, world.runs["resent"])
