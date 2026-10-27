"""Per-user spoiler boundaries.

A user's boundary in a show is the highest regular episode (in the show's order)
they have finished, where "finished" means at least WATCHED_PERCENT complete.
Rewatching an early episode never moves the boundary back.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Episode, Show, User, WatchEvent, WatchProgress
from .library import normalize_boundary, regular_episodes


def recompute_progress(
    session: Session, user_id: int, show_id: int, watched_percent: int
) -> WatchProgress | None:
    rows = session.execute(
        select(Episode, WatchEvent.watched_at)
        .join(WatchEvent, WatchEvent.episode_id == Episode.id)
        .where(
            WatchEvent.user_id == user_id,
            Episode.show_id == show_id,
            WatchEvent.percent_complete >= watched_percent,
        )
    ).all()
    progress = session.scalar(
        select(WatchProgress).where(
            WatchProgress.user_id == user_id, WatchProgress.show_id == show_id
        )
    )
    if not rows:
        if progress is not None:
            session.delete(progress)
        return None
    regular = [ep for ep, _ in rows if ep.season > 0]
    last_at = max(at for _, at in rows)
    boundary = None
    if regular:
        boundary = normalize_boundary(session, max(regular, key=lambda e: e.order_key))
    if progress is None:
        progress = WatchProgress(user_id=user_id, show_id=show_id)
        session.add(progress)
    progress.last_watched_episode_id = boundary.id if boundary else None
    progress.last_watched_at = last_at
    session.flush()
    return progress


def boundary_episode(session: Session, user_id: int, show_id: int) -> Episode | None:
    progress = session.scalar(
        select(WatchProgress).where(
            WatchProgress.user_id == user_id, WatchProgress.show_id == show_id
        )
    )
    if progress is None or progress.last_watched_episode_id is None:
        return None
    return session.get(Episode, progress.last_watched_episode_id)


def next_episode(session: Session, show_id: int, boundary: Episode | None) -> Episode | None:
    episodes = regular_episodes(session, show_id)
    if boundary is None:
        return episodes[0] if episodes else None
    return next((e for e in episodes if e.order_key > boundary.order_key), None)


def shows_in_progress(session: Session, user: User, now: datetime) -> list[dict]:
    rows = session.execute(
        select(WatchProgress, Show)
        .join(Show, Show.id == WatchProgress.show_id)
        .where(WatchProgress.user_id == user.id, WatchProgress.last_watched_episode_id.is_not(None))
        .order_by(WatchProgress.last_watched_at.desc())
    ).all()
    out = []
    for progress, show in rows:
        boundary = session.get(Episode, progress.last_watched_episode_id)
        nxt = next_episode(session, show.id, boundary)
        days = (now - progress.last_watched_at).days if progress.last_watched_at else None
        out.append(
            {
                "show_id": show.id,
                "title": show.title,
                "year": show.year,
                "has_poster": bool(show.poster_url),
                "last_watched_episode": boundary.code if boundary else None,
                "last_watched_episode_id": boundary.id if boundary else None,
                "last_watched_title": boundary.title if boundary else None,
                "next_episode": nxt.code if nxt else None,
                "next_episode_title": nxt.title if nxt else None,
                "last_watched_at": progress.last_watched_at.isoformat() + "Z"
                if progress.last_watched_at
                else None,
                "days_since": days,
            }
        )
    return out
