"""Evidence export: ``GET /v1/assurance/runs/{id}/evidence`` (B-34: FR-13.1).

Returns the run's evidence bundle as a ZIP (``rai.core.evidence``): the run and its
pins, steps, observations, lineage, recovery measurements, the scenario approvals it
ran under, its own audit-chain entries and the anchor, and ``report.md``: the same run
for a person to read, with what the evidence does not show (B-45), all under a SHA-256
manifest. Only
a finished run has one: its results never change after that, so two exports are
byte-identical. Each export is itself an audit entry (``evidence.export``), naming
who took it and the manifest's SHA-256, which is also the ``X-RAI-Manifest-SHA256``
header.

**Signed (B-35: FR-13.2).** With ``RAI_EVIDENCE_SIGNING_KEY`` set, the API adds
``signature.json``, an Ed25519 signature over the manifest, and names the key in the
``X-RAI-Signature-Key-Id`` header and the audit entry. ``GET /v1/evidence/public-key``
returns the public key; pin it from a source you trust and check bundles with
``rai verify --key``.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import Connection, text

from rai.api.context import ROLES, TenantSession, require, settings_of
from rai.api.problems import Problem
from rai.api.routes_runs import find_run
from rai.core import audit, evidence, report

router = APIRouter(prefix="/v1", tags=["evidence"])

reader = require(*ROLES, key_scope="runs:read")
anyone = require(*ROLES)
FINISHED = ("passed", "failed", "refused", "aborted", "error")


def _rows(conn: Connection, sql: str, run_id: UUID) -> list[dict[str, Any]]:
    return [r._asdict() for r in conn.execute(text(sql), {"r": run_id}).all()]


def collect(
    conn: Connection, run_id: UUID, signer: evidence.EvidenceSigner | None = None
) -> dict[str, Any]:
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
        "signature": evidence.signature_note(signer),
    }  # fmt: skip
    members: dict[str, Any] = {
        "run.json": run,
        "steps.json": steps,
        "observations.json": observations,
        "lineage.json": lineage,
        "recovery.json": recovery,
        "approvals.json": approvals,
        "audit.json": entries,
    }
    bundle["limitations"] = report.limitations(members, signed=signer is not None)
    return {"bundle.json": bundle, **members, "report.md": report.render(members, bundle)}


@router.get(
    "/assurance/runs/{run_id}/evidence",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}, "description": "the evidence bundle"}},
)
def export_evidence(
    run_id: UUID, request: Request, session: Annotated[TenantSession, Depends(reader)]
) -> Response:
    """The run's evidence bundle (ZIP). Only for a finished run; byte-identical each time."""
    run = find_run(session, run_id)
    if run.state not in FINISHED:
        raise Problem(409, "evidence.run_unfinished", f"The run is {run.state}",
                      detail="its evidence is exported once it finishes")  # fmt: skip
    signer = settings_of(request).evidence_signer
    bundle = evidence.build(run_id, collect(session.conn, run_id, signer), signer)
    if session.key is not None:
        actor_type, actor_id = "api_key", str(session.key.api_key_id)
    else:
        actor_type, actor_id = "user", str(session.user_id)
    audit.append(
        session.conn, tenant_id=session.tenant_id, actor_type=actor_type, actor_id=actor_id,
        action="evidence.export", subject_type="evidence_bundle", subject_id=str(run_id),
        payload={"manifest_sha256": bundle.manifest_sha256, "files": bundle.files,
                 "key_id": bundle.key_id},
    )  # fmt: skip
    return Response(
        bundle.data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.name}.zip"',
            "X-RAI-Manifest-SHA256": bundle.manifest_sha256,
            "Cache-Control": "no-store",
        }
        | ({"X-RAI-Signature-Key-Id": bundle.key_id} if bundle.key_id else {}),
    )


class PublicKeyOut(BaseModel):
    algorithm: str
    key_id: str
    public_key: str  # raw 32-byte Ed25519 key, standard base64


@router.get("/evidence/public-key")
def evidence_public_key(
    request: Request, session: Annotated[TenantSession, Depends(anyone)]
) -> PublicKeyOut:
    """The key that signs evidence bundles. Pin it from a source you trust: a key read from
    the server whose bundles you check proves only that the server signed them."""
    signer = settings_of(request).evidence_signer
    if signer is None:
        raise Problem(404, "evidence.unsigned", "This server does not sign evidence bundles",
                      detail="its operator sets RAI_EVIDENCE_SIGNING_KEY")  # fmt: skip
    return PublicKeyOut(algorithm="ed25519", key_id=signer.key_id, public_key=signer.public_key)
