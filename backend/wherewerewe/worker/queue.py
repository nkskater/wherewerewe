"""A small database-backed job queue.

Jobs run in lanes so a quick recap never waits behind an hour of Whisper:
- "interactive": recaps and history syncs
- "index": subtitle extraction, summaries, rollups, library scans
Claiming is an UPDATE ... WHERE status='queued' guarded by rowcount, which works the
same on SQLite and Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Job

LANES: dict[str, str] = {
    "generate_recap": "interactive",
    "sync_history": "interactive",
    "sync_library": "index",
    "index_show": "index",
    "index_episode": "index",
    "rollup_season": "index",
    "backfill": "index",
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def enqueue(
    session: Session,
    job_type: str,
    payload: dict | None = None,
    *,
    priority: int = 0,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> Job | None:
    """Add a job. With a dedupe_key, returns the existing job if one is queued or running."""
    if job_type not in LANES:
        raise ValueError(f"unknown job type {job_type!r}")
    if dedupe_key:
        existing = session.scalar(
            select(Job)
            .where(Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running")))
            .limit(1)
        )
        if existing is not None:
            if existing.status == "queued" and priority > existing.priority:
                existing.priority = priority
            return existing
    job = Job(
        type=job_type,
        queue=LANES[job_type],
        payload=payload or {},
        priority=priority,
        dedupe_key=dedupe_key,
        run_after=run_after or _now(),
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    return job


def claim(session: Session, lane: str) -> Job | None:
    for _ in range(5):
        candidate = session.scalar(
            select(Job)
            .where(Job.status == "queued", Job.queue == lane, Job.run_after <= _now())
            .order_by(Job.priority.desc(), Job.id)
            .limit(1)
        )
        if candidate is None:
            return None
        result = session.execute(
            update(Job)
            .where(Job.id == candidate.id, Job.status == "queued")
            .values(status="running", started_at=_now(), attempts=Job.attempts + 1, error=None)
        )
        session.commit()
        if result.rowcount == 1:
            session.refresh(candidate)
            return candidate
    return None


def complete(session: Session, job: Job, message: str | None = None) -> None:
    job.status = "done"
    job.finished_at = _now()
    job.progress = 1.0
    if message is not None:
        job.message = message


def fail(session: Session, job: Job, error: str, *, retry: bool = True) -> None:
    job.error = error[-4000:]
    job.finished_at = _now()
    if retry and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = _now() + timedelta(seconds=30 * 2 ** (job.attempts - 1))
    else:
        job.status = "failed"


def cancel_queued(session: Session, lane: str | None = None) -> int:
    """Cancel jobs that haven't started. Running jobs finish."""
    query = update(Job).where(Job.status == "queued")
    if lane:
        query = query.where(Job.queue == lane)
    count = session.execute(query.values(status="cancelled", finished_at=_now())).rowcount
    session.flush()
    return count


def reset_stale(session: Session) -> int:
    """Requeue jobs left 'running' by a worker that died."""
    result = session.execute(
        update(Job).where(Job.status == "running").values(status="queued", run_after=_now())
    )
    return result.rowcount or 0


def prune(session: Session, keep_days: int = 14) -> None:
    from sqlalchemy import delete

    session.execute(
        delete(Job).where(
            Job.status.in_(("done", "cancelled")),
            Job.finished_at < _now() - timedelta(days=keep_days),
        )
    )
