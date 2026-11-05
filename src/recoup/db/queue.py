"""A Postgres job queue: no Redis, no broker, one table.

Workers claim with `SELECT ... FOR UPDATE SKIP LOCKED`, so any number of them can poll
the same table without two taking one job. On SQLite the lock clause is dropped; there
is only ever one writer there anyway.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from recoup.db.models import Job, now
from recoup.db.session import Database

MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=15)
"""A running job whose worker has not finished it within the lease is handed to another worker."""

Handler = Callable[[str, dict[str, Any]], dict[str, Any]]
"""(tenant_id, payload) -> result."""


def enqueue(db: Database, tenant_id: str, kind: str, payload: dict[str, Any]) -> str:
    with db.session(tenant_id) as session:
        job = Job(tenant_id=tenant_id, kind=kind, payload=payload)
        session.add(job)
        session.flush()
        return job.id


def claim(db: Database) -> Job | None:
    """Take the oldest ready job and mark it running, atomically."""
    with db.session(db.SYSTEM) as session:
        query = (
            select(Job)
            .where(Job.status.in_(["queued", "running"]), Job.run_after <= now())
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = session.scalars(query).first()
        if job is None:
            return None
        job.status = "running"
        job.attempts += 1
        job.run_after = now() + LEASE  # doubles as the lease expiry while running
        session.flush()
        session.expunge(job)
        return job


def finish(db: Database, job_id: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    with db.session(db.SYSTEM) as session:
        job = session.get(Job, job_id)
        assert job is not None
        if error is None:
            job.status, job.result = "done", result
        elif job.attempts >= MAX_ATTEMPTS:
            job.status, job.error = "failed", error
        else:
            job.status, job.error = "queued", error
            job.run_after = now() + timedelta(seconds=2**job.attempts)


def run_one(db: Database, handlers: dict[str, Handler]) -> bool:
    """Process at most one job. Returns False when the queue is empty."""
    job = claim(db)
    if job is None:
        return False
    try:
        result = handlers[job.kind](job.tenant_id, job.payload)
    except Exception as error:  # a job failure must not kill the worker
        finish(db, job.id, error=f"{type(error).__name__}: {error}")
    else:
        finish(db, job.id, result=result)
    return True
