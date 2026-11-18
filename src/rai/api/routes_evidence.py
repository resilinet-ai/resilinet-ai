"""Evidence export: ``GET /v1/assurance/runs/{id}/evidence`` (B-34: FR-13.1).

Returns the run's evidence bundle as a ZIP (``rai.core.evidence``): the run and its
pins, steps, observations, lineage, recovery measurements, the scenario approvals it
ran under, its own audit-chain entries and the anchor, with a SHA-256 manifest. Only
a finished run has one: its results never change after that, so two exports are
byte-identical. Each export is itself an audit entry (``evidence.export``), naming
who took it and the manifest's SHA-256, which is also the ``X-RAI-Manifest-SHA256``
header.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy import Connection, text

from rai.api.context import ROLES, TenantSession, require
from rai.api.problems import Problem
from rai.api.routes_runs import find_run
from rai.core import audit, evidence

router = APIRouter(prefix="/v1/assurance/runs", tags=["evidence"])

reader = require(*ROLES, key_scope="runs:read")
FINISHED = ("passed", "failed", "refused", "aborted", "error")


def _rows(conn: Connection, sql: str, run_id: UUID) -> list[dict[str, Any]]:
    return [r._asdict() for r in conn.execute(text(sql), {"r": run_id}).all()]


def collect(conn: Connection, run_id: UUID) -> dict[str, Any]:
    """Every bundle member except the manifest, in a stable order."""
    run = conn.execute(
        text("SELECT run_id, tenant_id, project_id, plan_id, environment_id, state::text, "
             "verdict::text, reason, triggered_by, api_key_id, trigger_ref, rai_version, "
             "rerun_of, queued_at, started_at, finished_at, pins FROM rai.runs "
             "WHERE run_id = :r"),
        {"r": run_id},
    ).one()._asdict()  # fmt: skip
    steps = _rows(conn, "SELECT step_key, plan_position, state::text, verdict::text, error, "
                  "attempt, started_at, finished_at FROM rai.run_steps WHERE run_id = :r "
                  "ORDER BY plan_position, step_key", run_id)  # fmt: skip
    observations = _rows(conn, "SELECT observation_id, step_key, test_id, asset_id, kind::text, "
                         "rule, outcome::text, row_count, failing_count, observed_value, "
                         "expected, sample_ref, observed_at FROM rai.observations "
                         "WHERE run_id = :r ORDER BY observation_id", run_id)  # fmt: skip
    lineage = _rows(conn, "SELECT edge_id, step_key, from_asset_id, from_name, from_digest, "
                    "from_rows, to_asset_id, to_digest, transform_ref, recorded_at "
                    "FROM rai.lineage_edges WHERE run_id = :r ORDER BY edge_id",
                    run_id)  # fmt: skip
    recovery = _rows(conn, "SELECT measurement_id, step_key, scenario_id, method, onset_at, "
                     "reverted_at, restored_at, rto_seconds, rto_objective_seconds, rto_met, "
                     "rpo_attempted, rpo_seconds, rpo_objective_seconds, rpo_met, lost_rows, "
                     "stable_checks, window_seconds, recorded_at FROM rai.recovery_measurements "
                     "WHERE run_id = :r ORDER BY measurement_id", run_id)  # fmt: skip
    approvals = _rows(conn, "SELECT a.approval_id, a.scenario_id, a.reviewer_id, "
                      "a.decision::text, a.comment, a.created_at FROM rai.scenario_approvals a "
                      "WHERE a.approval_id IN (SELECT (i -> 'scenario' ->> 'approval_id')::uuid "
                      "  FROM rai.runs r, jsonb_array_elements(r.pins -> 'items') i "
                      "  WHERE r.run_id = :r AND i ? 'scenario' "
                      "  AND jsonb_typeof(i -> 'scenario') = 'object') "
                      "ORDER BY a.approval_id", run_id)  # fmt: skip
    entries = _rows(conn, "SELECT seq, actor_type::text, actor_id, action, subject_type, "
                    "subject_id, occurred_at, payload::text AS payload, prev_hash, record_hash "
                    "FROM rai.audit_chain WHERE subject_type = 'run' "
                    "AND subject_id = CAST(:r AS text) ORDER BY seq", run_id)  # fmt: skip
    if not entries:  # every run has its insert audited; none means it was removed
        raise Problem(409, "evidence.no_audit_trail", "The run has no audit entries")
    last = entries[-1]
    bundle = {
        "format": evidence.FORMAT,
        "scope": {"kind": "run", "run_id": run_id, "project_id": run["project_id"],
                  "tenant_id": run["tenant_id"]},
        "anchor": {"seq": last["seq"], "record_hash": last["record_hash"],
                   "action": last["action"], "occurred_at": last["occurred_at"]},
        "counts": {"steps": len(steps), "observations": len(observations),
                   "lineage_edges": len(lineage), "recovery_measurements": len(recovery),
                   "scenario_approvals": len(approvals), "audit_entries": len(entries)},
        "excluded": ["synthetic data (its SHA-256 digests are in lineage.json)",
                     "secret values (pins hold secret references only)"],
        "statement": evidence.STATEMENT,
        "signature": None,  # B-35 (FR-13.2) signs the manifest
    }  # fmt: skip
    return {
        "bundle.json": bundle,
        "run.json": run,
        "steps.json": steps,
        "observations.json": observations,
        "lineage.json": lineage,
        "recovery.json": recovery,
        "approvals.json": approvals,
        "audit.json": entries,
    }


@router.get(
    "/{run_id}/evidence",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}, "description": "the evidence bundle"}},
)
def export_evidence(run_id: UUID, session: Annotated[TenantSession, Depends(reader)]) -> Response:
    """The run's evidence bundle (ZIP). Only for a finished run; byte-identical each time."""
    run = find_run(session, run_id)
    if run.state not in FINISHED:
        raise Problem(409, "evidence.run_unfinished", f"The run is {run.state}",
                      detail="its evidence is exported once it finishes")  # fmt: skip
    bundle = evidence.build(run_id, collect(session.conn, run_id))
    if session.key is not None:
        actor_type, actor_id = "api_key", str(session.key.api_key_id)
    else:
        actor_type, actor_id = "user", str(session.user_id)
    audit.append(
        session.conn, tenant_id=session.tenant_id, actor_type=actor_type, actor_id=actor_id,
        action="evidence.export", subject_type="evidence_bundle", subject_id=str(run_id),
        payload={"manifest_sha256": bundle.manifest_sha256, "files": bundle.files},
    )  # fmt: skip
    return Response(
        bundle.data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.name}.zip"',
            "X-RAI-Manifest-SHA256": bundle.manifest_sha256,
            "Cache-Control": "no-store",
        },
    )
