from datetime import UTC, datetime, timedelta

import pytest

from wherewerewe.db import session_scope
from wherewerewe.models import Job
from wherewerewe.worker import queue
from wherewerewe.worker.runner import run_job


def test_dedupe_and_priority():
    with session_scope() as s:
        a = queue.enqueue(s, "index_episode", {"episode_id": 1}, dedupe_key="ep:1")
        b = queue.enqueue(s, "index_episode", {"episode_id": 1}, dedupe_key="ep:1", priority=9)
        assert a.id == b.id and a.priority == 9
        queue.enqueue(s, "index_episode", {"episode_id": 2}, priority=1)
        queue.enqueue(s, "generate_recap", {"recap_id": 1})
    with session_scope() as s:
        first = queue.claim(s, "index")
        assert first.payload == {"episode_id": 1} and first.status == "running"
        assert first.attempts == 1
        assert queue.claim(s, "index").payload == {"episode_id": 2}
        assert queue.claim(s, "index") is None
        assert queue.claim(s, "interactive").type == "generate_recap"


def test_run_after_is_respected():
    with session_scope() as s:
        later = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
        queue.enqueue(s, "sync_history", {}, run_after=later)
    with session_scope() as s:
        assert queue.claim(s, "interactive") is None


def test_failures_retry_with_backoff_then_fail(monkeypatch):
    from wherewerewe.worker import jobs

    monkeypatch.setitem(
        jobs.HANDLERS, "backfill", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with session_scope() as s:
        job_id = queue.enqueue(s, "backfill", {}, max_attempts=2).id
    for attempt in (1, 2):
        with session_scope() as s:
            job = s.get(Job, job_id)
            job.run_after = job.created_at
            s.flush()
            assert queue.claim(s, "index").id == job_id
        run_job(job_id)
        with session_scope() as s:
            job = s.get(Job, job_id)
            assert "boom" in job.error
            assert job.status == ("queued" if attempt == 1 else "failed")


def test_permanent_errors_do_not_retry(monkeypatch):
    from wherewerewe.worker import jobs

    monkeypatch.setitem(
        jobs.HANDLERS, "backfill", lambda *a: (_ for _ in ()).throw(jobs.PermanentError("no subs"))
    )
    with session_scope() as s:
        job_id = queue.enqueue(s, "backfill", {}).id
    with session_scope() as s:
        queue.claim(s, "index")
    run_job(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == "failed" and job.error == "no subs" and job.attempts == 1


def test_reset_stale_and_unknown_type():
    with session_scope() as s:
        queue.enqueue(s, "backfill", {})
    with session_scope() as s:
        queue.claim(s, "index")
    with session_scope() as s:
        assert queue.reset_stale(s) == 1
    with session_scope() as s, pytest.raises(ValueError):
        queue.enqueue(s, "nope", {})
