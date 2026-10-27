"""Job handlers."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from ..config import Settings
from ..llm.client import get_llm
from ..llm.rollup import build_rollup
from ..models import Job, Recap
from ..services import indexing, recaps, sync
from . import queue

log = logging.getLogger(__name__)


class PermanentError(RuntimeError):
    """A failure retrying won't fix (missing file, no subtitles)."""


def _sync_library(session: Session, settings: Settings, job: Job) -> str:
    result = sync.sync_library(session, settings)
    return f"{result['shows']} shows, {result['episodes']} episodes"


def _sync_history(session: Session, settings: Settings, job: Job) -> str:
    result = sync.sync_history(
        session,
        settings,
        full=bool(job.payload.get("full")),
        plex_user_id=job.payload.get("plex_user_id"),
    )
    active = set(indexing.active_show_ids(session, indexing.AUTO_INDEX_ACTIVE_DAYS))
    for show_id in {show_id for _, show_id in result["affected"]} & active:
        queue.enqueue(
            session,
            "index_show",
            {"show_id": show_id},
            priority=1,
            dedupe_key=f"index_show:{show_id}",
        )
    return f"{result['rows']} history rows, {len(result['affected'])} user/show pairs updated"


def _index_show(session: Session, settings: Settings, job: Job) -> str:
    count = indexing.queue_show_index(
        session,
        int(job.payload["show_id"]),
        scope=job.payload.get("scope", "watched"),
        force=bool(job.payload.get("force")),
        model=indexing.summary_model(settings),
    )
    return f"queued {count} episodes"


def _index_episode(session: Session, settings: Settings, job: Job) -> str:
    from ..sources.resolver import NoSourceError

    try:
        return indexing.index_episode(
            session, settings, int(job.payload["episode_id"]), force=bool(job.payload.get("force"))
        )
    except (NoSourceError, LookupError) as exc:
        raise PermanentError(str(exc)) from exc


def _rollup_season(session: Session, settings: Settings, job: Job) -> str:
    row = build_rollup(
        session, get_llm(settings), int(job.payload["show_id"]), int(job.payload["season"])
    )
    return f"season {row.season} rollup from {row.episode_count} episodes"


def _generate_recap(session: Session, settings: Settings, job: Job) -> str:
    recap_id = int(job.payload["recap_id"])
    session.commit()  # run_recap uses its own short transactions for streaming
    try:
        recap = recaps.run_recap(recap_id, notify=bool(job.payload.get("notify")))
    except Exception as exc:
        recap_row = session.get(Recap, recap_id)
        if recap_row is not None:
            session.refresh(recap_row)
            final = job.attempts >= job.max_attempts
            recap_row.status = "failed" if final else "queued"
            recap_row.error = str(exc)
            session.commit()
        raise
    return f"recap {recap.id}: {recap.status}"


def _backfill(session: Session, settings: Settings, job: Job) -> str:
    shows = indexing.active_show_ids(session, indexing.BACKFILL_ACTIVE_DAYS)
    for show_id in shows:
        queue.enqueue(
            session,
            "index_show",
            {"show_id": show_id},
            priority=0,
            dedupe_key=f"index_show:{show_id}",
        )
    return f"queued indexing for {len(shows)} shows"


HANDLERS: dict[str, Callable[[Session, Settings, Job], str]] = {
    "sync_library": _sync_library,
    "sync_history": _sync_history,
    "index_show": _index_show,
    "index_episode": _index_episode,
    "rollup_season": _rollup_season,
    "generate_recap": _generate_recap,
    "backfill": _backfill,
}
