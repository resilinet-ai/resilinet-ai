"""The evidence report and its limitations statement (B-45: FR-13.1, NFR-AUD-2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rai.core import evidence, report

RUN = UUID("01900000-0000-7000-8000-000000000001")
T = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
IMAGE = "sha256:" + "c" * 64


def members(**changes: Any) -> dict[str, Any]:
    environment = {"environment_id": str(RUN), "name": "SANDBOX-01", "type": "SANDBOX",
                   "images": {"payments-api": IMAGE}, "unpinned_images": []}  # fmt: skip
    scenario = {"name": "ledger outage", "version": 2, "approval_id": str(RUN),
                "faults": [{"type": "container_stop", "target": "payments-api"}]}  # fmt: skip
    run = {"run_id": RUN, "project_id": RUN, "state": "passed", "verdict": "pass",
           "reason": None, "triggered_by": RUN, "api_key_id": None, "rerun_of": None,
           "rai_version": "0.9.0", "queued_at": T, "started_at": T, "finished_at": T,
           "pins": {"format": "rai-run-pins/1", "plan": {"name": "nightly", "version": 3},
                    "environment": environment,
                    "items": [{"position": 1, "step_key": "item-1",
                               "scenario": scenario}]}}  # fmt: skip
    base: dict[str, Any] = {
        "run.json": run,
        "steps.json": [{"step_key": "item-1", "state": "done", "verdict": "pass", "error": None}],
        "observations.json": [{"outcome": "pass"}, {"outcome": "pass"}],
        "lineage.json": [],
        "approvals.json": [],
        "audit.json": [],
        "recovery.json": [],
    }
    return base | changes


def test_a_complete_signed_pinned_run_states_only_what_always_holds() -> None:
    limits = report.limitations(members(), signed=True)
    assert limits[0].startswith("The run executed in environment SANDBOX-01 (SANDBOX).")
    assert limits[1:] == [report.LIMIT_SCOPE, report.LIMIT_EXCLUDED, evidence.STATEMENT]


def test_each_limitation_follows_from_the_bundle() -> None:
    m = members()
    m["run.json"] = m["run.json"] | {"state": "aborted"}
    m["steps.json"] = [{"step_key": "item-1", "state": "error", "verdict": None, "error": "x"}]
    m["recovery.json"] = [{"step_key": "item-1", "rto_seconds": None}]
    limits = report.limitations(m, signed=False)
    assert any("did not complete (state aborted)" in limit for limit in limits)
    assert any(limit == "1 step(s) ended in error and gave no verdict: item-1."
               for limit in limits)  # fmt: skip
    assert report.LIMIT_RECOVERY in limits
    assert report.LIMIT_UNSIGNED in limits
    assert limits[-1] == evidence.STATEMENT


def test_images_that_were_not_pinned_are_named() -> None:
    m = members()
    env = m["run.json"]["pins"]["environment"]
    env.update(images={}, unpinned_images=["payments-api", "etl-job"])
    limits = report.limitations(m, signed=True)
    assert any(
        limit.startswith("Container images were not pinned for payments-api ") for limit in limits
    )  # fmt: skip  (etl-job is not acted on: not named)
    del env["images"]  # a run queued before B-45
    assert any("predates image pinning" in limit for limit in report.limitations(m, signed=True))
    m["run.json"]["pins"]["environment"] = None
    assert report.limitations(m, signed=True)[0].startswith("The run had no environment")


def test_the_report_is_markdown_that_no_value_can_break() -> None:
    m = members()
    m["steps.json"] = [{"step_key": "item-1", "state": "error", "verdict": None,
                        "error": "bad | row\nsecond line"}]  # fmt: skip
    m["recovery.json"] = [{"step_key": "item-1", "rto_seconds": "12.5",
                           "rto_objective_seconds": 30, "rto_met": True}]  # fmt: skip
    bundle = {"anchor": {"seq": 7, "record_hash": "ab" * 32, "action": "run.finish"},
              "signature": None, "limitations": report.limitations(m, signed=False)}  # fmt: skip
    text = report.render(m, bundle)
    assert text.startswith(f"# RAI evidence report: run {RUN}\n")
    assert text.endswith("\n")
    assert "\r" not in text
    assert "| item-1 | scenario | ledger outage v2 | error | - | bad \\| row second line |" in text
    assert "| Environment | SANDBOX-01 (SANDBOX) |" in text
    assert f"| payments-api | {IMAGE} |" in text
    assert "| item-1 | 12.5 | 30 | True |" in text
    assert "Observations: pass 2 (observations.json)." in text
    assert "- Not signed." in text
    assert all(f"- {limit}" in text for limit in bundle["limitations"])
    assert report.render(m, bundle) == text  # deterministic


def test_text_members_are_stored_with_lf_endings() -> None:
    bundle = evidence.build(RUN, {"report.md": "a\r\nb\r\n\n", "x.json": {"k": 1}})
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(bundle.data)) as archive:
        assert archive.read(f"rai-evidence-{RUN}/report.md") == b"a\nb\n"
    assert evidence.verify(bundle.data).ok
