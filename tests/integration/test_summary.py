"""The executive dashboard's numbers (FR-11.1, NFR-USE-1): every number with its
denominator, window and definition, computed from the project's own rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain
from tests.integration.test_findings import Story, failing, open_on
from tests.integration.test_scenario_recovery import DROP, RADIUS, System, run, setup
from tests.integration.test_scenarios import Chaos, chaos

pytestmark = needs_db
__all__ = ["chaos", "setup"]  # fixtures shared with the scenario tests

URL = "/v1/assurance/summary"


def summary(client: TestClient, lab: Lab, **params: Any) -> Any:
    response = client.get(URL, params={"project_id": lab.project, **params}, headers=lab.viewer)
    assert response.status_code == 200, response.text
    return response.json()


def check_counts(owner: Engine, project: str) -> tuple[int, int, int]:
    """Passed, judged and failing rule checks, counted straight from the tables."""
    with owner.connect() as conn:
        row = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE o.outcome = 'pass'), "
                "count(*) FILTER (WHERE o.outcome <> 'skipped'), "
                "count(*) FILTER (WHERE o.outcome IN ('fail', 'error')) "
                "FROM rai.observations o JOIN rai.runs r USING (run_id) "
                "WHERE r.project_id = :p AND o.kind = 'rule_result'"
            ),
            {"p": project},
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


def test_an_empty_project_has_zero_denominators_and_every_definition(
    lab: Lab, client: TestClient
) -> None:
    body = summary(client, lab)
    for ratio in ("validation", "refused", "chaos"):
        assert (body[ratio]["numerator"], body[ratio]["denominator"]) == (0, 0)
        assert body[ratio]["definition"]
    assert (body["rto"]["met"], body["rto"]["with_objective"]) == (0, 0)
    assert body["issues"] == {"total": 0, "rules": [], "definition": body["issues"]["definition"]}
    assert body["runs"]["finished"] == 0
    assert body["runs"]["by_state"] == {}
    assert body["findings"]["open"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}
    assert body["scenarios"] == []
    since = datetime.fromisoformat(body["since"])
    until = datetime.fromisoformat(body["until"])
    assert until - since == timedelta(days=30)
    assert "UTC" in body["window_definition"]


def test_validation_runs_and_findings_are_counted_in_their_window(
    lab: Lab, client: TestClient, feed: Path, worker_engine: Engine, owner: Engine,
    tmp_path: Path,
) -> None:  # fmt: skip
    story = Story(lab, feed, worker_engine, tmp_path)
    dirty = story.run()
    story.clean()
    story.run()
    open_on(client, lab, dirty, failing(owner, dirty))  # critical

    body = summary(client, lab)
    passed, judged, fails = check_counts(owner, lab.project)
    assert fails > 0
    assert judged > passed
    assert (body["validation"]["numerator"], body["validation"]["denominator"]) == (passed, judged)
    assert body["issues"]["total"] == fails
    counts = [r["failing_checks"] for r in body["issues"]["rules"]]
    assert counts == sorted(counts, reverse=True)
    assert sum(counts) <= fails
    assert body["runs"] == {"finished": 2, "by_state": {"failed": 1, "passed": 1},
                            "definition": body["runs"]["definition"]}  # fmt: skip
    assert (body["refused"]["numerator"], body["refused"]["denominator"]) == (0, 2)
    assert body["findings"]["open"]["critical"] == 1
    assert body["findings"]["opened_in_window"] == 1

    # A window that ends before the runs sees none of them; open findings stay.
    earlier = summary(client, lab, until=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    assert earlier["validation"]["denominator"] == 0
    assert earlier["runs"]["finished"] == 0
    assert earlier["findings"]["open"]["critical"] == 1
    assert earlier["findings"]["opened_in_window"] == 0


def test_scenarios_recovery_and_refusals(
    setup: tuple[Chaos, System], client: TestClient, worker_engine: Engine, feed: Path,
    tmp_path: Path,
) -> None:  # fmt: skip
    chaos, system = setup
    system.heads.append({"seq": 104, "committed_at": "2026-11-17T12:00:30Z", "rows": 104})
    done, _ = run(chaos, worker_engine, feed, tmp_path, rto_seconds=30, rpo_seconds=0)
    assert done["verdict"] == "pass"
    # A worker without a signing key refuses a scenario run in preflight.
    sid = chaos.scenario(DROP, blast_radius=RADIUS)["scenario_id"]
    chaos.review(sid)
    refused = chaos.lab.start(chaos.plan(sid))["run_id"]
    drain(Worker(worker_engine, config(feed, tmp_path / "no-key")))
    assert chaos.lab.run(refused)["state"] == "refused"

    body = summary(client, chaos.lab)
    assert (body["chaos"]["numerator"], body["chaos"]["denominator"]) == (1, 1)
    [latest] = body["scenarios"]
    assert (latest["run_id"], latest["step_key"], latest["verdict"]) == (
        done["run_id"], "item-1", "pass",
    )  # fmt: skip
    assert (latest["rto_met"], latest["rpo_met"]) == (True, True)
    assert (body["rto"]["met"], body["rto"]["with_objective"]) == (1, 1)
    assert (body["rpo"]["met"], body["rpo"]["with_objective"]) == (1, 1)
    assert body["recovery_measurements"] == 1
    assert (body["refused"]["numerator"], body["refused"]["denominator"]) == (1, 2)
    assert body["runs"]["by_state"] == {"passed": 1, "refused": 1}


def test_access_and_window_checks(lab: Lab, client: TestClient, owner: Engine) -> None:
    other = Lab(client, owner)
    wrong = client.get(URL, params={"project_id": other.project}, headers=lab.viewer)
    assert wrong.status_code == 404
    for params, code in (
        ({"since": "2026-11-10T00:00:00Z", "until": "2026-11-01T00:00:00Z"}, "window_empty"),
        ({"since": "2024-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"}, "window_too_long"),
    ):
        response = client.get(URL, params={"project_id": lab.project, **params},
                              headers=lab.viewer)  # fmt: skip
        assert response.status_code == 422
        assert response.json()["code"] == f"summary.{code}"
    assert client.get(URL, params={"project_id": lab.project}).status_code == 401
