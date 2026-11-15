"""The PostgreSQL job queue, seen from the worker (B-18: FR-06.2, Architecture section 5).

These statements run as ``rai_worker`` without a tenant context: its one RLS
exemption lets it see and lease every tenant's jobs, and nothing else. A claim
takes a lease; a worker that dies stops renewing it, so after the lease expires
another worker claims the job and resumes the run. A job is attempted at most
``max_attempts`` times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import Engine, text

BACKOFF = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class Job:
    job_id: UUID
    tenant_id: UUID
    run_id: UUID
    attempt: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class Exhausted:
    job_id: UUID
    tenant_id: UUID
    run_id: UUID
    attempts: int
    last_error: str | None


def claim(engine: Engine, worker: str, lease: timedelta) -> Job | None:
    """Lease the next available job (FOR UPDATE SKIP LOCKED), or None."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE rai.jobs SET locked_by = :w, attempts = attempts + 1, "
                "lease_expires_at = clock_timestamp() + :lease "
                "WHERE job_id = ("
                "  SELECT job_id FROM rai.jobs WHERE finished_at IS NULL "
                "  AND available_at <= clock_timestamp() AND attempts < max_attempts "
                "  AND (locked_by IS NULL OR lease_expires_at < clock_timestamp()) "
                "  ORDER BY available_at, job_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING job_id, tenant_id, run_id, attempts, max_attempts"
            ),
            {"w": worker, "lease": lease},
        ).first()
    if row is None:
        return None
    return Job(row.job_id, row.tenant_id, row.run_id, row.attempts, row.max_attempts)


def renew(engine: Engine, job: Job, worker: str, lease: timedelta) -> bool:
    """Extend the lease; False if another worker has taken the job over."""
    with engine.begin() as conn:
        renewed = conn.execute(
            text(
                "UPDATE rai.jobs SET lease_expires_at = clock_timestamp() + :lease "
                "WHERE job_id = :j AND locked_by = :w AND attempts = :a AND finished_at IS NULL "
                "RETURNING job_id"
            ),
            {"j": job.job_id, "w": worker, "a": job.attempt, "lease": lease},
        ).first()
    return renewed is not None


def finish(engine: Engine, job: Job, worker: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rai.jobs SET finished_at = clock_timestamp(), locked_by = NULL, "
                "lease_expires_at = NULL WHERE job_id = :j AND locked_by = :w"
            ),
            {"j": job.job_id, "w": worker},
        )


def release(engine: Engine, job: Job, worker: str, error: str) -> None:
    """Give the job back after a failure; it becomes available again after a backoff."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rai.jobs SET locked_by = NULL, lease_expires_at = NULL, last_error = :e, "
                "available_at = clock_timestamp() + :wait WHERE job_id = :j AND locked_by = :w"
            ),
            {"j": job.job_id, "w": worker, "e": error[:2000], "wait": BACKOFF * job.attempt},
        )


def exhausted(engine: Engine, limit: int = 20) -> list[Exhausted]:
    """Unfinished jobs that used every attempt and are not leased any more."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT job_id, tenant_id, run_id, attempts, last_error FROM rai.jobs "
                "WHERE finished_at IS NULL AND attempts >= max_attempts "
                "AND (locked_by IS NULL OR lease_expires_at < clock_timestamp()) "
                "ORDER BY job_id LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"n": limit},
        ).all()
    return [Exhausted(r.job_id, r.tenant_id, r.run_id, r.attempts, r.last_error) for r in rows]


def close_exhausted(engine: Engine, job_id: UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rai.jobs SET finished_at = clock_timestamp(), locked_by = NULL, "
                "lease_expires_at = NULL WHERE job_id = :j AND finished_at IS NULL"
            ),
            {"j": job_id},
        )
