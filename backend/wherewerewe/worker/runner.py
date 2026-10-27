"""Worker process: one thread per lane plus a scheduler."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta

from ..db import create_all, get_state, load_settings, session_scope, set_state
from ..models import Job
from . import queue
from .jobs import HANDLERS, PermanentError

log = logging.getLogger(__name__)


def run_job(job_id: int) -> None:
    """Run one claimed job to completion, recording the outcome."""
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        settings = load_settings(session)
        started = time.monotonic()
        try:
            handler = HANDLERS[job.type]
            message = handler(session, settings, job)
            job = session.get(Job, job_id)
            assert job is not None
            job.timings = {**(job.timings or {}), "seconds": round(time.monotonic() - started, 2)}
            queue.complete(session, job, message)
            log.info("job %s %s done: %s", job.id, job.type, message)
        except Exception as exc:
            session.rollback()
            job = session.get(Job, job_id)
            assert job is not None
            permanent = isinstance(exc, PermanentError)
            detail = str(exc) if permanent else f"{exc}\n{traceback.format_exc(limit=8)}"
            queue.fail(session, job, detail, retry=not permanent)
            job.timings = {**(job.timings or {}), "seconds": round(time.monotonic() - started, 2)}
            log.warning(
                "job %s %s failed (attempt %s/%s): %s",
                job.id,
                job.type,
                job.attempts,
                job.max_attempts,
                exc,
            )


def run_pending(lane: str | None = None, limit: int = 1000) -> int:
    """Run queued jobs inline until none are left (used by the CLI and tests)."""
    ran = 0
    lanes = [lane] if lane else sorted(set(queue.LANES.values()))
    while ran < limit:
        progressed = False
        for name in lanes:
            with session_scope() as session:
                job = queue.claim(session, name)
                job_id = job.id if job else None
            if job_id is not None:
                run_job(job_id)
                ran += 1
                progressed = True
        if not progressed:
            break
    return ran


class Worker:
    def __init__(self):
        self.stop = threading.Event()

    def lane_loop(self, lane: str) -> None:
        while not self.stop.is_set():
            try:
                with session_scope() as session:
                    poll = load_settings(session).worker_poll_seconds
                    job = queue.claim(session, lane)
                    job_id = job.id if job else None
                if job_id is None:
                    self.stop.wait(poll)
                    continue
                run_job(job_id)
            except Exception:
                log.exception("worker lane %s crashed; continuing", lane)
                self.stop.wait(5)

    def schedule_tick(self) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        with session_scope() as session:
            settings = load_settings(session)

            def due(key: str, minutes: int) -> bool:
                if minutes <= 0:
                    return False
                last = get_state(session, key)
                if last and now - datetime.fromisoformat(last) < timedelta(minutes=minutes):
                    return False
                set_state(session, key, now.isoformat())
                return True

            if settings.plex_url and due("sched_library", settings.library_scan_interval_minutes):
                queue.enqueue(session, "sync_library", {}, dedupe_key="sync_library")
            if (
                settings.tautulli_url
                and settings.tautulli_api_key
                and due("sched_history", settings.sync_interval_minutes)
            ):
                queue.enqueue(session, "sync_history", {}, dedupe_key="sync_history:")
            local = datetime.now()
            if settings.backfill_hour >= 0 and local.hour == settings.backfill_hour:
                today = local.date().isoformat()
                if get_state(session, "sched_backfill") != today:
                    set_state(session, "sched_backfill", today)
                    queue.enqueue(session, "backfill", {}, dedupe_key="backfill")
            if now.minute == 0:
                queue.prune(session)

    def scheduler_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.schedule_tick()
            except Exception:
                log.exception("scheduler tick failed")
            self.stop.wait(60)

    def run(self) -> None:
        create_all()
        with session_scope() as session:
            reset = queue.reset_stale(session)
            if reset:
                log.info("requeued %s jobs left running by a previous worker", reset)
        threads = [
            threading.Thread(target=self.lane_loop, args=(lane,), name=f"lane-{lane}", daemon=True)
            for lane in sorted(set(queue.LANES.values()))
        ]
        threads.append(threading.Thread(target=self.scheduler_loop, name="scheduler", daemon=True))
        for t in threads:
            t.start()
        log.info("worker started with lanes: %s", ", ".join(sorted(set(queue.LANES.values()))))
        try:
            while not self.stop.is_set():
                self.stop.wait(1)
        except KeyboardInterrupt:
            self.stop.set()
        for t in threads:
            t.join(timeout=10)
