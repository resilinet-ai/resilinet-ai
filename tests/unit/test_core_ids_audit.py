"""UUIDv7 and the audit-chain verifier, without a database (B-05: FR-01.2, NFR-AUD-2)."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from typer.testing import CliRunner

from rai.cli.main import app
from rai.core.audit import (
    GENESIS_HASH,
    AuditRecord,
    canonical,
    record_hash,
    verify_chain,
)
from rai.core.db import alembic_config, database_url, sqlalchemy_url
from rai.core.ids import uuid7, uuid7_unix_ms

TENANT = UUID("01890a5d-ac96-774b-bcce-b302099a8057")


def test_uuid7_layout() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"
    assert abs(uuid7_unix_ms(value) - time.time_ns() // 1_000_000) < 5_000


def test_uuid7_is_time_ordered() -> None:
    earlier, later = uuid7(1_700_000_000_000), uuid7(1_700_000_000_001)
    assert earlier < later
    assert uuid7_unix_ms(earlier) == 1_700_000_000_000


def test_uuid7_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="48 bits"):
        uuid7(1 << 48)
    with pytest.raises(ValueError, match="version-7"):
        uuid7_unix_ms(UUID(int=0))


def chain(length: int) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    prev = GENESIS_HASH
    start = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    for seq in range(1, length + 1):
        draft = AuditRecord(
            seq=seq,
            tenant_id=TENANT,
            actor_type="user",
            actor_id=f"user-{seq}",
            action="membership.insert",
            subject_type="membership",
            subject_id=str(seq),
            occurred_at=start + timedelta(seconds=seq),
            payload_text='{"new": {"role": "viewer"}}',
            prev_hash=prev,
            record_hash="",
        )
        record = replace(draft, record_hash=record_hash(prev, draft))
        records.append(record)
        prev = record.record_hash
    return records


def test_canonical_form_is_fixed() -> None:
    record = chain(1)[0]
    assert canonical(record) == (
        '["01890a5d-ac96-774b-bcce-b302099a8057",1,"user","user-1","membership.insert",'
        '"membership","1","2026-11-16T09:00:01.000000Z",{"new": {"role": "viewer"}}]'
    )


def test_intact_chain_verifies() -> None:
    records = chain(5)
    result = verify_chain(records)
    assert result.ok
    assert (result.checked, result.head_hash) == (5, records[-1].record_hash)
    assert verify_chain([]).ok


@pytest.mark.parametrize(
    ("tamper", "seq", "reason"),
    [
        (lambda c: c.__setitem__(2, replace(c[2], actor_id="mallory")), 3, "record_hash"),
        (lambda c: c.pop(2), 4, "prev_hash"),
        (lambda c: c.insert(3, c[2]), 3, "seq order"),
        (lambda c: c.__setitem__(0, replace(c[0], prev_hash="f" * 64)), 1, "prev_hash"),
    ],
    ids=["edited", "deleted", "replayed", "forged-genesis"],
)
def test_tampering_is_detected(tamper: object, seq: int, reason: str) -> None:
    records = chain(5)
    tamper(records)  # type: ignore[operator]
    result = verify_chain(records)
    assert not result.ok
    assert result.first_break_seq == seq
    assert reason in (result.reason or "")


def test_url_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sqlalchemy_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert sqlalchemy_url("postgres://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert sqlalchemy_url("sqlite://") == "sqlite://"
    monkeypatch.delenv("RAI_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="RAI_DATABASE_URL"):
        database_url()
    config = alembic_config("postgresql://u:p%40ss@h/d")
    assert config.get_main_option("sqlalchemy.url") == "postgresql+psycopg://u:p%40ss@h/d"


def test_cli_needs_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAI_DATABASE_URL", raising=False)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 2
    result = runner.invoke(app, ["audit", "verify", "--tenant", str(TENANT)])
    assert result.exit_code == 2
