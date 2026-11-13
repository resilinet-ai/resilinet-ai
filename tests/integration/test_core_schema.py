"""RAI core schema against a real PostgreSQL 16 (B-05: FR-01.1, FR-01.2, NFR-SEC-1, NFR-AUD-1/2).

Needs RAI_TEST_DSN (CI sets it). Tests create their own tenants instead of
truncating, because the audit chain refuses TRUNCATE by design.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from rai.cli.main import app
from rai.core import db
from rai.core.audit import AuditRecord, append, canonical, fetch_chain, verify_tenant
from rai.core.tenancy import set_context

DSN = os.environ.get("RAI_TEST_DSN", "")
if os.environ.get("CI") == "true" and not DSN:  # never skip silently in CI
    raise RuntimeError("RAI_TEST_DSN must be set in CI")
pytestmark = pytest.mark.skipif(not DSN, reason="RAI_TEST_DSN not set")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    # Prove the migration is reversible, then leave a fresh schema for the tests.
    db.upgrade(DSN)
    db.downgrade(DSN)
    db.upgrade(DSN)
    engine = create_engine(db.sqlalchemy_url(DSN))
    yield engine
    engine.dispose()


def new_tenant(engine: Engine) -> UUID:
    slug = f"t-{uuid4().hex[:12]}"
    with engine.begin() as conn:
        return UUID(
            str(
                conn.execute(
                    text(
                        "INSERT INTO rai.tenants (name, slug) VALUES (:n, :s) RETURNING tenant_id"
                    ),
                    {"n": slug, "s": slug},
                ).scalar_one()
            )
        )


def new_user(engine: Engine) -> UUID:
    with engine.begin() as conn:
        email = f"{uuid4().hex[:10]}@example.test"
        return UUID(
            str(
                conn.execute(
                    text(
                        "INSERT INTO rai.users (email, display_name) VALUES (:e, 'Test') "
                        "RETURNING user_id"
                    ),
                    {"e": email},
                ).scalar_one()
            )
        )


def add_member(engine: Engine, tenant: UUID, user: UUID, role: str = "engineer") -> None:
    with engine.begin() as conn:
        set_context(conn, tenant, "user", "admin-1")
        conn.execute(
            text(
                "INSERT INTO rai.memberships (tenant_id, user_id, role) "
                "VALUES (:t, :u, CAST(:r AS rai.member_role))"
            ),
            {"t": tenant, "u": user, "r": role},
        )


def count(conn: Connection, table: str) -> int:
    return int(conn.execute(text(f"SELECT count(*) FROM rai.{table}")).scalar_one())


def sql_verify(engine: Engine, tenant: UUID) -> tuple[Any, ...]:
    with engine.begin() as conn:
        row = conn.execute(text("SELECT * FROM rai.audit_verify(:t)"), {"t": tenant}).one()
        return tuple(row)


# ---- identifiers ---------------------------------------------------------------


def test_sql_uuid_v7_is_version_7_and_time_ordered(engine: Engine) -> None:
    with engine.begin() as conn:
        first = UUID(str(conn.execute(text("SELECT rai.uuid_v7()")).scalar_one()))
        time.sleep(0.005)
        second = UUID(str(conn.execute(text("SELECT rai.uuid_v7()")).scalar_one()))
    assert (first.version, second.version) == (7, 7)
    assert first.variant == "specified in RFC 4122"
    assert first < second
    assert abs((first.int >> 80) - time.time_ns() // 1_000_000) < 10_000


# ---- row-level security (NFR-SEC-1) ------------------------------------------


def test_tenants_only_see_their_own_rows(engine: Engine) -> None:
    a, b = new_tenant(engine), new_tenant(engine)
    alice, bob = new_user(engine), new_user(engine)
    add_member(engine, a, alice)
    add_member(engine, b, bob)

    with engine.begin() as conn:
        set_context(conn, a, "user", "alice")
        assert conn.execute(text("SELECT tenant_id FROM rai.tenants")).scalars().all() == [a]
        assert conn.execute(text("SELECT user_id FROM rai.memberships")).scalars().all() == [alice]
        assert conn.execute(text("SELECT user_id FROM rai.users")).scalars().all() == [alice]
        tenants_in_audit = conn.execute(text("SELECT DISTINCT tenant_id FROM rai.audit_chain"))
        assert tenants_in_audit.scalars().all() == [a]


def test_no_tenant_context_sees_nothing(engine: Engine) -> None:
    tenant = new_tenant(engine)
    add_member(engine, tenant, new_user(engine))
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE rai_app"))
        for table in ("tenants", "users", "memberships", "audit_chain"):
            assert count(conn, table) == 0, table


def test_writing_into_another_tenant_is_refused(engine: Engine) -> None:
    a, b = new_tenant(engine), new_tenant(engine)
    user = new_user(engine)
    with pytest.raises(DBAPIError, match="row-level security"), engine.begin() as conn:  # noqa: PT012
        set_context(conn, a, "user", "alice")
        conn.execute(
            text("INSERT INTO rai.memberships (tenant_id, user_id, role) VALUES (:t, :u, 'admin')"),
            {"t": b, "u": user},
        )


def test_app_role_cannot_create_tenants_or_bypass_rls(engine: Engine) -> None:
    tenant = new_tenant(engine)
    with pytest.raises(DBAPIError, match="permission denied"), engine.begin() as conn:  # noqa: PT012
        set_context(conn, tenant, "user", "alice")
        conn.execute(text("INSERT INTO rai.tenants (name, slug) VALUES ('x', 'xx-evil')"))
    with engine.begin() as conn:
        bypass = conn.execute(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'rai_app'"))
        assert bypass.scalar_one() is False


# ---- automatic, hash-chained audit (FR-01.2) ------------------------------------


def test_tenant_and_membership_changes_are_audited(engine: Engine) -> None:
    tenant, user = new_tenant(engine), new_user(engine)
    add_member(engine, tenant, user, "viewer")
    with engine.begin() as conn:
        set_context(conn, tenant, "user", "admin-1")
        conn.execute(
            text("UPDATE rai.memberships SET role = 'reviewer' WHERE user_id = :u"), {"u": user}
        )
        conn.execute(text("DELETE FROM rai.memberships WHERE user_id = :u"), {"u": user})
        conn.execute(text("UPDATE rai.tenants SET status = 'suspended'"))

    with engine.begin() as conn:
        set_context(conn, tenant, "system", "test")
        chain = fetch_chain(conn, tenant)
    actions = [(r.action, r.actor_type, r.actor_id) for r in chain]
    assert actions == [
        ("tenant.insert", "system", "unknown"),
        ("membership.insert", "user", "admin-1"),
        ("membership.update", "user", "admin-1"),
        ("membership.delete", "user", "admin-1"),
        ("tenant.update", "user", "admin-1"),
    ]
    assert chain[1].subject_id == str(user)
    assert '"role": "viewer"' in chain[2].payload_text
    assert '"role": "reviewer"' in chain[2].payload_text
    assert chain[0].prev_hash == "0" * 64
    assert all(later.prev_hash == earlier.record_hash for earlier, later in pairwise(chain))


def test_python_and_sql_verification_agree(engine: Engine) -> None:
    tenant = new_tenant(engine)
    add_member(engine, tenant, new_user(engine))
    with engine.begin() as conn:
        set_context(conn, tenant, "agent", "chaos-agent")
        append(
            conn,
            tenant_id=tenant,
            actor_type="agent",
            actor_id='quote " backslash \\ tab \t ctrl \x01 é 中',
            action="fault.inject",
            subject_type="injection",
            subject_id="S03",
            payload={"text": 'line\nbreak "q" \\ \x02 é', "n": 1.50, "nested": {"b": [1, 2]}},
        )
        result = verify_tenant(conn, tenant)
        sql_text = conn.execute(
            text(
                "SELECT rai.audit_canonical(a) FROM rai.audit_chain a "
                "WHERE tenant_id = :t ORDER BY seq DESC LIMIT 1"
            ),
            {"t": tenant},
        ).scalar_one()
        latest = fetch_chain(conn, tenant)[-1]
    assert canonical(latest) == sql_text  # byte-for-byte, including escapes and Unicode
    assert result.ok
    assert result.checked == 3
    assert sql_verify(engine, tenant) == (3, None, None)


def test_forged_chain_fields_are_overwritten(engine: Engine) -> None:
    tenant = new_tenant(engine)
    with engine.begin() as conn:
        set_context(conn, tenant, "user", "mallory")
        conn.execute(
            text(
                "INSERT INTO rai.audit_chain (seq, tenant_id, actor_type, actor_id, action, "
                "subject_type, subject_id, prev_hash, record_hash, occurred_at) VALUES "
                "(1, :t, 'user', 'mallory', 'x', 'y', 'z', :h, :h, '2000-01-01')"
            ),
            {"t": tenant, "h": "a" * 64},
        )
        latest = fetch_chain(conn, tenant)[-1]
    assert latest.seq != 1
    assert latest.prev_hash != "a" * 64
    assert latest.occurred_at.year >= 2026
    assert sql_verify(engine, tenant)[1] is None


def test_audit_chain_is_insert_only(engine: Engine) -> None:
    tenant = new_tenant(engine)
    with pytest.raises(DBAPIError, match="permission denied"), engine.begin() as conn:  # noqa: PT012
        set_context(conn, tenant, "user", "alice")
        conn.execute(text("UPDATE rai.audit_chain SET actor_id = 'x'"))
    for statement in (
        "UPDATE rai.audit_chain SET actor_id = 'x' WHERE tenant_id = :t",
        "DELETE FROM rai.audit_chain WHERE tenant_id = :t",
        "TRUNCATE rai.audit_chain",
    ):
        with pytest.raises(DBAPIError, match="insert-only"), engine.begin() as conn:
            conn.execute(text(statement), {"t": tenant})  # as owner: the trigger still refuses


def test_audited_tenant_cannot_be_deleted(engine: Engine) -> None:
    tenant = new_tenant(engine)
    with pytest.raises(DBAPIError, match="foreign key"), engine.begin() as conn:
        conn.execute(text("DELETE FROM rai.tenants WHERE tenant_id = :t"), {"t": tenant})


def tamper(engine: Engine, statement: str, params: dict[str, Any]) -> None:
    """What only a database owner could do: bypass the insert-only trigger."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE rai.audit_chain DISABLE TRIGGER audit_chain_insert_only"))
        conn.execute(text(statement), params)
        conn.execute(text("ALTER TABLE rai.audit_chain ENABLE TRIGGER audit_chain_insert_only"))


def audited_tenant(engine: Engine) -> tuple[UUID, list[AuditRecord]]:
    tenant = new_tenant(engine)
    for role in ("viewer", "engineer", "auditor"):
        add_member(engine, tenant, new_user(engine), role)
    with engine.begin() as conn:
        set_context(conn, tenant, "system", "test")
        return tenant, fetch_chain(conn, tenant)


def test_edited_record_is_detected_by_both_verifiers(engine: Engine) -> None:
    tenant, chain = audited_tenant(engine)
    target = chain[2].seq
    tamper(
        engine,
        'UPDATE rai.audit_chain SET payload = \'{"new": {"role": "admin"}}\' WHERE seq = :s',
        {"s": target},
    )
    with engine.begin() as conn:
        set_context(conn, tenant, "system", "test")
        result = verify_tenant(conn, tenant)
    assert (result.first_break_seq, result.reason) == (
        target,
        "record_hash does not match the record content",
    )
    assert sql_verify(engine, tenant)[1:] == (
        target,
        "record_hash does not match the record content",
    )


def test_deleted_record_is_detected(engine: Engine) -> None:
    tenant, chain = audited_tenant(engine)
    tamper(engine, "DELETE FROM rai.audit_chain WHERE seq = :s", {"s": chain[1].seq})
    with engine.begin() as conn:
        set_context(conn, tenant, "system", "test")
        result = verify_tenant(conn, tenant)
    assert result.first_break_seq == chain[2].seq
    assert result.reason == "prev_hash does not match the previous record"

    runner = CliRunner()
    broken = runner.invoke(
        app, ["audit", "verify", "--tenant", str(tenant)], env={"RAI_DATABASE_URL": DSN}
    )
    assert broken.exit_code == 1
    assert "BROKEN" in broken.output


def test_concurrent_writers_keep_one_valid_chain(engine: Engine) -> None:
    tenant = new_tenant(engine)

    def write(worker: int) -> None:
        for n in range(20):
            with engine.begin() as conn:
                set_context(conn, tenant, "worker", f"w{worker}")
                append(
                    conn,
                    tenant_id=tenant,
                    actor_type="worker",
                    actor_id=f"w{worker}",
                    action="job.step",
                    subject_type="job",
                    subject_id=f"{worker}-{n}",
                )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    with engine.begin() as conn:
        set_context(conn, tenant, "system", "test")
        result = verify_tenant(conn, tenant)
    assert result.ok
    assert result.checked == 1 + 8 * 20

    runner = CliRunner()
    ok = runner.invoke(
        app, ["audit", "verify", "--tenant", str(tenant)], env={"RAI_DATABASE_URL": DSN}
    )
    assert ok.exit_code == 0
    assert "tamper-evident, not tamper-proof" in ok.output


# ---- reference data rules ---------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("INSERT INTO rai.users (email, display_name) VALUES ('not-an-email', 'x')", "check"),
        ("INSERT INTO rai.tenants (name, slug) VALUES ('x', 'Bad Slug')", "check"),
        ("INSERT INTO rai.memberships VALUES (rai.uuid_v7(), rai.uuid_v7(), 'owner')", "enum"),
    ],
)
def test_invalid_reference_data_is_rejected(engine: Engine, statement: str, message: str) -> None:
    with pytest.raises(DBAPIError, match=message), engine.begin() as conn:
        conn.execute(text(statement))


def test_email_is_unique_regardless_of_case(engine: Engine) -> None:
    email = f"Case-{uuid4().hex[:8]}@Example.test"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO rai.users (email, display_name) VALUES (:e, 'x')"), {"e": email}
        )
    with pytest.raises(DBAPIError, match="unique"), engine.begin() as conn:
        conn.execute(
            text("INSERT INTO rai.users (email, display_name) VALUES (:e, 'y')"),
            {"e": email.lower()},
        )


def test_one_role_per_tenant(engine: Engine) -> None:
    tenant, user = new_tenant(engine), new_user(engine)
    add_member(engine, tenant, user, "viewer")
    with pytest.raises(DBAPIError, match="duplicate key"):
        add_member(engine, tenant, user, "admin")


def test_set_context_needs_a_transaction(engine: Engine) -> None:
    with engine.connect() as conn, pytest.raises(RuntimeError, match="open transaction"):
        set_context(conn, uuid4(), "system", "test")


def test_cli_upgrade_is_idempotent(engine: Engine) -> None:
    result = CliRunner().invoke(app, ["db", "upgrade"], env={"RAI_DATABASE_URL": DSN})
    assert result.exit_code == 0
    assert "upgraded to head" in result.output
