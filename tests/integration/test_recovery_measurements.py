"""Append-only recovery measurements and their summary (B-29: FR-08.1, FR-08.2, NFR-AUD-1).

Uses the fake system under test of ``test_scenario_recovery``: health follows the
fake proxy the agent toggles, and the ledger head is set by each test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab
from tests.integration.test_scenario_recovery import DROP, RADIUS, System, run, setup
from tests.integration.test_scenarios import Chaos, chaos

pytestmark = needs_db
__all__ = ["chaos", "setup"]  # fixtures shared with the scenario tests

URL = "/v1/assurance/recovery"


def measurements(owner: Engine, run_id: str) -> list[dict[str, Any]]:
    with owner.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM rai.recovery_measurements WHERE run_id = :r"), {"r": run_id}
        ).all()
    return [dict(r._mapping) for r in rows]


def summary(client: TestClient, lab: Lab, **params: Any) -> Any:
    response = client.get(URL, params={"project_id": lab.project, **params}, headers=lab.viewer)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_measurement_is_recorded_with_its_step(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path,
    owner: Engine,
) -> None:  # fmt: skip
    chaos, system = setup
    system.heads.append({"seq": 104, "committed_at": "2026-11-17T12:00:30Z", "rows": 104})
    done, seen = run(chaos, worker_engine, feed, tmp_path, rto_seconds=30, rpo_seconds=0)
    assert done["verdict"] == "pass"
    [row] = measurements(owner, done["run_id"])
    rto = seen["item-1/recovery.rto"][2]
    assert float(row["rto_seconds"]) == rto["seconds"]  # the same number as the observation
    assert (row["rto_objective_seconds"], row["rto_met"]) == (30, True)
    assert (row["rpo_attempted"], float(row["rpo_seconds"]), row["lost_rows"]) == (True, 0.0, 0)
    assert (row["rpo_objective_seconds"], row["rpo_met"]) == (0, True)
    assert row["step_key"] == "item-1"
    assert row["onset_at"] <= row["reverted_at"] <= row["restored_at"]
    assert (row["method"], row["stable_checks"], row["window_seconds"]) == ("recovery-1", 2, 20)
    with owner.connect() as conn:  # committed with the step it measures
        step = conn.execute(
            text("SELECT state::text FROM rai.run_steps WHERE run_id = :r AND step_key = :k"),
            {"r": done["run_id"], "k": "item-1"},
        ).scalar_one()
    assert step == "done"


def test_misses_are_recorded_and_counted_against_their_objectives(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path,
    owner: Engine, client: TestClient,
) -> None:  # fmt: skip
    chaos, system = setup
    system.heads.append({"seq": 104, "committed_at": "2026-11-17T12:00:30Z", "rows": 104})
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    met, _ = run(chaos, worker_engine, feed, tmp_path / "a", rto_seconds=30, rpo_seconds=0)
    system.healthy = lambda: False  # never restored this time
    system.heads = [{"seq": 104, "committed_at": "2026-11-17T12:00:30Z", "rows": 104},
                    {"seq": 90, "committed_at": "2026-11-17T11:59:00Z", "rows": 90}]  # fmt: skip
    missed, _ = run(chaos, worker_engine, feed, tmp_path / "b", rto_seconds=30, rpo_seconds=0,
                    window_seconds=1)  # fmt: skip
    assert (met["verdict"], missed["verdict"]) == ("pass", "fail")
    [row] = measurements(owner, missed["run_id"])
    assert (row["restored_at"], row["rto_seconds"], row["rto_met"]) == (None, None, False)
    assert (row["lost_rows"], row["rpo_met"]) == (14, False)

    out = summary(client, chaos.lab)
    assert (out["measurements"], out["not_restored"]) == (2, 1)
    assert (out["rto"]["met"], out["rto"]["with_objective"]) == (1, 2)
    assert (out["rpo"]["met"], out["rpo"]["with_objective"]) == (1, 2)
    assert "stable_checks" in out["rto"]["definition"]
    assert [m["run_id"] for m in out["items"]] == [missed["run_id"], met["run_id"]]  # newest first
    assert out["items"][0]["scenario_name"]
    assert not out["truncated"]
    assert summary(client, chaos.lab, limit=1)["truncated"]


def test_no_objective_no_count(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path,
    client: TestClient,
) -> None:  # fmt: skip
    chaos, _ = setup
    run(chaos, worker_engine, feed, tmp_path, ledger=None)  # no objectives, no ledger
    out = summary(client, chaos.lab)
    assert out["measurements"] == 1
    assert out["rto"]["with_objective"] == out["rpo"]["with_objective"] == 0
    [item] = out["items"]
    assert (item["rto_met"], item["rpo_attempted"], item["rpo_seconds"]) == (None, False, None)


def test_measurements_are_insert_only(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path,
    owner: Engine,
) -> None:  # fmt: skip
    """T-03: a measurement cannot be edited or removed, even by the schema owner."""
    chaos, _ = setup
    done, _ = run(chaos, worker_engine, feed, tmp_path, rto_seconds=30)
    for statement in ("UPDATE rai.recovery_measurements SET rto_met = true WHERE run_id = :r",
                      "DELETE FROM rai.recovery_measurements WHERE run_id = :r",
                      "TRUNCATE rai.recovery_measurements"):  # fmt: skip
        with pytest.raises(DBAPIError, match="insert-only"), owner.begin() as conn:
            conn.execute(text(statement), {"r": done["run_id"]})
    assert len(measurements(owner, done["run_id"])) == 1


def test_a_scenario_without_a_recovery_plan_has_no_measurement(
    chaos: Chaos, worker_engine: Engine, feed: Path, tmp_path: Path, owner: Engine
) -> None:
    sid = chaos.scenario(DROP, blast_radius=RADIUS)["scenario_id"]
    chaos.review(sid)
    started = chaos.lab.start(chaos.plan(sid))
    assert chaos.worker(worker_engine, feed, tmp_path, probe_interval=0.05).run_once()
    assert chaos.lab.run(started["run_id"])["verdict"] == "pass"
    assert measurements(owner, started["run_id"]) == []


def test_the_window_and_tenancy(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path,
    client: TestClient, owner: Engine,
) -> None:  # fmt: skip
    chaos, _ = setup
    run(chaos, worker_engine, feed, tmp_path, rto_seconds=30)
    lab = chaos.lab
    later = datetime.now(UTC) + timedelta(hours=1)
    after = summary(client, lab, since=later.isoformat(),
                    until=(later + timedelta(hours=1)).isoformat())  # fmt: skip
    assert after["measurements"] == 0
    assert summary(client, lab, until=(later - timedelta(days=1)).isoformat())["measurements"] == 0
    bad = client.get(URL, params={"project_id": lab.project, "since": later.isoformat(),
                                  "until": later.isoformat()}, headers=lab.viewer)  # fmt: skip
    assert (bad.status_code, bad.json()["code"]) == (422, "recovery.window_empty")
    long = client.get(URL, params={"project_id": lab.project, "since": "2020-01-01T00:00:00Z"},
                      headers=lab.viewer)  # fmt: skip
    assert long.json()["code"] == "recovery.window_too_long"

    other = Lab(client, owner)  # another tenant sees nothing of this project
    hidden = client.get(URL, params={"project_id": lab.project}, headers=other.viewer)
    assert hidden.status_code == 404
    assert summary(client, other)["measurements"] == 0


def test_a_run_without_post_recovery_checks_has_none(
    setup: tuple[Chaos, System], worker_engine: Engine, feed: Path, tmp_path: Path,
    client: TestClient,
) -> None:  # fmt: skip
    chaos, _ = setup
    run(chaos, worker_engine, feed, tmp_path, rto_seconds=30)
    out = summary(client, chaos.lab)
    [item] = out["items"]
    assert (item["consistency"], item["rpo_truth"]) == (None, None)
    assert (out["consistency"]["met"], out["consistency"]["with_objective"]) == (0, 0)
