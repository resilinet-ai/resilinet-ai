"""Audit hash chain: canonical form and an independent verifier (FR-01.2, NFR-AUD-2).

Each tenant has one chain. The database computes every hash on insert
(``rai.audit_chain_link``); this module recomputes the chain outside the database
so a reviewer does not have to trust the database's own verification.

What a passing check means, stated honestly (NFR-AUD-2): the stored records have
not been altered, removed or reordered since they were chained, *unless* someone
with database superuser rights rewrote the record and every later hash. Comparing
the current chain head with a head exported earlier (for example inside an
evidence bundle) exposes that. The chain is tamper-evident, not tamper-proof,
and it does not prove the recorded facts were true.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, text

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditRecord:
    seq: int
    tenant_id: UUID
    actor_type: str
    actor_id: str
    action: str
    subject_type: str
    subject_id: str
    occurred_at: datetime
    payload_text: str
    prev_hash: str
    record_hash: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    checked: int
    first_break_seq: int | None = None
    reason: str | None = None
    head_hash: str = GENESIS_HASH

    @property
    def ok(self) -> bool:
        return self.first_break_seq is None


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def canonical(record: AuditRecord) -> str:
    """Canonical text of one entry; must equal SQL ``rai.audit_canonical`` exactly.

    ``payload_text`` is PostgreSQL's own ``jsonb`` text output, used verbatim.
    """
    stamp = record.occurred_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    fields = [
        _json_string(str(record.tenant_id)),
        str(record.seq),
        _json_string(record.actor_type),
        _json_string(record.actor_id),
        _json_string(record.action),
        _json_string(record.subject_type),
        _json_string(record.subject_id),
        _json_string(stamp),
        record.payload_text,
    ]
    return "[" + ",".join(fields) + "]"


def record_hash(prev_hash: str, record: AuditRecord) -> str:
    return hashlib.sha256((prev_hash + canonical(record)).encode("utf-8")).hexdigest()


def verify_chain(records: Iterable[AuditRecord]) -> VerifyResult:
    """Check links and hashes of one tenant's chain, given in ``seq`` order."""
    expected_prev = GENESIS_HASH
    checked = 0
    last_seq: int | None = None
    for record in records:
        checked += 1
        if last_seq is not None and record.seq <= last_seq:
            return VerifyResult(checked, record.seq, "records are not in seq order", expected_prev)
        last_seq = record.seq
        if record.prev_hash != expected_prev:
            return VerifyResult(
                checked, record.seq, "prev_hash does not match the previous record", expected_prev
            )
        if record.record_hash != record_hash(record.prev_hash, record):
            return VerifyResult(
                checked, record.seq, "record_hash does not match the record content", expected_prev
            )
        expected_prev = record.record_hash
    return VerifyResult(checked, head_hash=expected_prev)


def fetch_chain(conn: Connection, tenant_id: UUID) -> list[AuditRecord]:
    """One tenant's chain in seq order (the caller's RLS context applies)."""
    rows = conn.execute(
        text(
            "SELECT seq, tenant_id, actor_type::text, actor_id, action, subject_type, subject_id, "
            "occurred_at, payload::text, prev_hash, record_hash "
            "FROM rai.audit_chain WHERE tenant_id = :tenant ORDER BY seq"
        ),
        {"tenant": tenant_id},
    )
    return [AuditRecord(*row) for row in rows]


def verify_tenant(conn: Connection, tenant_id: UUID) -> VerifyResult:
    return verify_chain(fetch_chain(conn, tenant_id))


def append(
    conn: Connection,
    *,
    tenant_id: UUID,
    actor_type: str,
    actor_id: str,
    action: str,
    subject_type: str,
    subject_id: str,
    payload: dict[str, object] | None = None,
) -> int:
    """Append an explicit audit entry; the database assigns seq and hashes. Returns seq."""
    seq = conn.execute(
        text(
            "INSERT INTO rai.audit_chain (tenant_id, actor_type, actor_id, action, subject_type, "
            "subject_id, payload) "
            "VALUES (:tenant, CAST(:actor_type AS rai.actor_type), :actor_id, :action, "
            ":subject_type, :subject_id, CAST(:payload AS jsonb)) "
            "RETURNING seq"
        ),
        {
            "tenant": tenant_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "payload": json.dumps(payload or {}),
        },
    ).scalar_one()
    return int(seq)
