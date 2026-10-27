"""Tautulli webhook handling and "returning after a gap" detection."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..integrations import notifiers
from ..models import Episode, Recap, Show, User, WatchProgress
from ..worker import queue
from .library import regular_episodes

log = logging.getLogger(__name__)

PLAY_ACTIONS = {"play", "playback.start", "media.play", "start"}
DONE_ACTIONS = {"stop", "watched", "playback.stop", "media.stop", "media.scrobble"}
RENOTIFY_AFTER = timedelta(hours=12)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _find_episode(session: Session, payload: dict) -> tuple[Show | None, Episode | None]:
    show = None
    if payload.get("grandparent_rating_key"):
        show = session.scalar(
            select(Show).where(Show.plex_rating_key == str(payload["grandparent_rating_key"]))
        )
    episode = None
    if payload.get("rating_key"):
        episode = session.scalar(
            select(Episode).where(Episode.plex_rating_key == str(payload["rating_key"]))
        )
        if episode is not None and show is None:
            show = session.get(Show, episode.show_id)
    if episode is None and show is not None:
        try:
            season, number = int(payload.get("season_num")), int(payload.get("episode_num"))
        except (TypeError, ValueError):
            return show, None
        episode = session.scalar(
            select(Episode).where(
                Episode.show_id == show.id, Episode.season == season, Episode.episode == number
            )
        )
    return show, episode


def handle_webhook(session: Session, payload: dict, settings: Settings) -> dict:
    action = str(payload.get("action") or payload.get("event") or "").lower()
    media_type = str(payload.get("media_type") or "episode").lower()
    if media_type != "episode":
        return {"result": "ignored", "reason": f"media_type {media_type}"}

    plex_user_id = str(payload.get("user_id") or "")
    if action in DONE_ACTIONS:
        # Refresh progress from Tautulli shortly after a stop/watched event.
        job = queue.enqueue(
            session,
            "sync_history",
            {"plex_user_id": plex_user_id or None},
            dedupe_key=f"sync_history:{plex_user_id}",
            run_after=_now() + timedelta(seconds=20),
        )
        return {"result": "sync_queued", "job_id": job.id if job else None}
    if action not in PLAY_ACTIONS:
        return {"result": "ignored", "reason": f"action {action or 'missing'}"}

    user = (
        session.scalar(select(User).where(User.plex_user_id == plex_user_id))
        if plex_user_id
        else None
    )
    show, episode = _find_episode(session, payload)
    if user is None or show is None:
        # Unknown user or show: sync so we know about them next time.
        queue.enqueue(session, "sync_history", {}, dedupe_key="sync_history:")
        return {"result": "unknown", "reason": "user or show not synced yet"}

    if settings.auto_index:
        # Someone started watching: make sure the show is indexed (cheap if already done).
        queue.enqueue(
            session,
            "index_show",
            {"show_id": show.id},
            priority=5,
            dedupe_key=f"index_show:{show.id}",
        )

    progress = session.scalar(
        select(WatchProgress).where(
            WatchProgress.user_id == user.id, WatchProgress.show_id == show.id
        )
    )
    if (
        progress is None
        or progress.last_watched_at is None
        or progress.last_watched_episode_id is None
    ):
        return {"result": "no_history", "reason": "first time watching this show"}

    if not user.notifications_enabled or not notifiers.targets_for(user.notify_target, settings):
        return {"result": "no_target", "reason": "nowhere to send a recap"}

    threshold = user.gap_threshold_days or settings.default_gap_days
    gap = _now() - progress.last_watched_at
    if gap < timedelta(days=threshold):
        return {"result": "no_gap", "gap_days": gap.days, "threshold_days": threshold}

    boundary = session.get(Episode, progress.last_watched_episode_id)
    if (
        episode is not None
        and episode.season > 0
        and boundary is not None
        and episode.order_key <= boundary.order_key
    ):
        # Rewatching something already seen: recap only what came before it.
        earlier = [e for e in regular_episodes(session, show.id) if e.order_key < episode.order_key]
        if not earlier:
            return {"result": "no_recap", "reason": "starting from the first episode"}
        boundary = earlier[-1]
    if boundary is None:
        return {"result": "no_recap", "reason": "no boundary"}

    recent = session.scalar(
        select(Recap)
        .where(
            Recap.user_id == user.id,
            Recap.show_id == show.id,
            Recap.trigger == "webhook",
            Recap.created_at > _now() - RENOTIFY_AFTER,
        )
        .limit(1)
    )
    if recent is not None:
        return {"result": "already_sent", "recap_id": recent.id}

    recap = Recap(
        user_id=user.id,
        show_id=show.id,
        boundary_episode_id=boundary.id,
        length="quick",
        trigger="webhook",
        status="queued",
    )
    session.add(recap)
    session.flush()
    queue.enqueue(
        session,
        "generate_recap",
        {"recap_id": recap.id, "notify": True},
        priority=100,
        dedupe_key=f"generate_recap:{recap.id}",
    )
    log.info(
        "user %s returned to %s after %s days; recap %s queued",
        user.display_name,
        show.title,
        gap.days,
        recap.id,
    )
    return {"result": "recap_queued", "recap_id": recap.id, "gap_days": gap.days}
