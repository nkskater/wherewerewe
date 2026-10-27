"""Plex library scans, Tautulli history sync, and local folder imports."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import get_state, set_state
from ..integrations.plex import PlexClient, PlexError
from ..integrations.tautulli import TautulliClient
from ..models import Episode, Show, User, WatchEvent
from ..sources.subtitles import SUBTITLE_EXTENSIONS
from .library import link_multi_episode_files
from .progress import recompute_progress

log = logging.getLogger(__name__)


def plex_client(settings: Settings) -> PlexClient:
    return PlexClient(settings.plex_url, settings.plex_token, verify=settings.verify_ssl)


def tautulli_client(settings: Settings) -> TautulliClient:
    return TautulliClient(
        settings.tautulli_url, settings.tautulli_api_key, verify=settings.verify_ssl
    )


def upsert_episode(session: Session, show: Show, season: int, number: int, **fields) -> Episode:
    rating_key = fields.get("plex_rating_key")
    ep = None
    if rating_key:
        ep = session.scalar(select(Episode).where(Episode.plex_rating_key == rating_key))
        if ep is not None and (ep.show_id, ep.season, ep.episode) != (show.id, season, number):
            # Plex renumbered or moved this item; free the key so the right row can take it.
            ep.plex_rating_key = None
            session.flush()
            ep = None
    if ep is None:
        ep = session.scalar(
            select(Episode).where(
                Episode.show_id == show.id, Episode.season == season, Episode.episode == number
            )
        )
    if ep is None:
        ep = Episode(show_id=show.id, season=season, episode=number)
        session.add(ep)
    for key, value in fields.items():
        if value is not None:
            setattr(ep, key, value)
    session.flush()
    return ep


def sync_show_from_plex(session: Session, plex: PlexClient, show: Show) -> int:
    assert show.plex_rating_key
    count = 0
    for pe in plex.episodes(show.plex_rating_key):
        upsert_episode(
            session,
            show,
            pe.season,
            pe.episode,
            plex_rating_key=pe.rating_key,
            title=pe.title,
            plex_summary=pe.summary,
            duration_ms=pe.duration_ms,
            air_date=pe.air_date,
            file_path=pe.file_path,
        )
        count += 1
    link_multi_episode_files(session, show.id)
    return count


def upsert_plex_show(session: Session, ps) -> Show:
    show = session.scalar(select(Show).where(Show.plex_rating_key == ps.rating_key))
    if show is None:
        show = Show(plex_rating_key=ps.rating_key, title=ps.title)
        session.add(show)
    show.title = ps.title
    show.year = ps.year
    show.poster_url = ps.thumb
    if ps.tmdb_id:
        show.tmdb_id = ps.tmdb_id
    session.flush()
    return show


def sync_library(session: Session, settings: Settings) -> dict:
    plex = plex_client(settings)
    wanted = {x.strip().lower() for x in settings.plex_libraries.split(",") if x.strip()}
    shows = episodes = 0
    try:
        for section in plex.show_sections():
            if wanted and section["title"].lower() not in wanted:
                continue
            for ps in plex.shows(section["key"]):
                show = upsert_plex_show(session, ps)
                episodes += sync_show_from_plex(session, plex, show)
                shows += 1
                session.commit()
    finally:
        plex.close()
    return {"shows": shows, "episodes": episodes}


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def upsert_user(
    session: Session, plex_user_id: str, display_name: str | None, username: str | None = None
) -> User:
    user = session.scalar(select(User).where(User.plex_user_id == plex_user_id))
    if user is None:
        user = User(
            plex_user_id=plex_user_id,
            display_name=display_name or username or f"User {plex_user_id}",
            username=username,
        )
        session.add(user)
        session.flush()
    elif display_name and user.display_name != display_name:
        user.display_name = display_name
    return user


def _show_for_row(session: Session, row: dict, plex: PlexClient | None) -> Show | None:
    key = str(row.get("grandparent_rating_key") or "")
    if not key:
        return None
    show = session.scalar(select(Show).where(Show.plex_rating_key == key))
    if show is not None:
        return show
    if plex is not None:
        try:
            ps = plex.show(key)
        except PlexError as exc:  # Plex may no longer have it; fall back to a stub
            log.info("could not fetch show %s from Plex: %s", key, exc)
            ps = None
        if ps is not None:
            show = upsert_plex_show(session, ps)
            try:
                sync_show_from_plex(session, plex, show)
            except PlexError as exc:  # e.g. a deleted show whose metadata lingers
                log.info("could not fetch episodes of show %s from Plex: %s", key, exc)
            return show
    show = Show(plex_rating_key=key, title=row.get("grandparent_title") or f"Show {key}")
    session.add(show)
    session.flush()
    return show


def sync_history(
    session: Session, settings: Settings, *, full: bool = False, plex_user_id: str | None = None
) -> dict:
    """Pull episode history from Tautulli and recompute affected boundaries."""
    tautulli = tautulli_client(settings)
    plex = plex_client(settings) if settings.plex_url else None
    cursor = get_state(session, "tautulli_history_synced")
    after: date | None = None
    if cursor and not full:
        # Re-read a couple of days: grouped rows keep growing as sessions are resumed.
        after = (datetime.fromisoformat(cursor) - timedelta(days=2)).date()
    started = datetime.now(UTC).replace(tzinfo=None)
    affected: set[tuple[int, int]] = set()
    rows = 0
    try:
        for row in tautulli.iter_history(after=after, user_id=plex_user_id):
            if row.get("media_type") not in (None, "episode"):
                continue
            season, number = _int(row.get("parent_media_index")), _int(row.get("media_index"))
            user_id = row.get("user_id")
            if season is None or number is None or user_id in (None, ""):
                continue
            show = _show_for_row(session, row, plex)
            if show is None:
                continue
            user = upsert_user(session, str(user_id), row.get("friendly_name"), row.get("user"))
            rating_key = str(row["rating_key"]) if row.get("rating_key") else None
            ep = None
            if rating_key:
                ep = session.scalar(select(Episode).where(Episode.plex_rating_key == rating_key))
            if ep is None:
                ep = upsert_episode(
                    session,
                    show,
                    season,
                    number,
                    title=row.get("title"),
                    plex_rating_key=rating_key,
                )
            ts = _int(row.get("stopped")) or _int(row.get("date"))
            if ts is None:
                continue
            watched_at = datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)
            percent = _int(row.get("percent_complete")) or 0
            ref = str(row.get("reference_id") or row.get("id") or "") or None
            event = (
                session.scalar(select(WatchEvent).where(WatchEvent.tautulli_ref == ref))
                if ref
                else None
            )
            if event is None:
                event = WatchEvent(
                    user_id=user.id,
                    episode_id=ep.id,
                    watched_at=watched_at,
                    percent_complete=percent,
                    tautulli_ref=ref,
                )
                session.add(event)
            else:
                event.user_id, event.episode_id = user.id, ep.id
                event.watched_at = max(event.watched_at, watched_at)
                event.percent_complete = max(event.percent_complete, percent)
            affected.add((user.id, show.id))
            rows += 1
            if rows % 500 == 0:
                session.flush()
        session.flush()
        for user_id, show_id in affected:
            recompute_progress(session, user_id, show_id, settings.watched_percent)
        if plex_user_id is None:
            set_state(session, "tautulli_history_synced", started.isoformat())
    finally:
        tautulli.close()
        if plex is not None:
            plex.close()
    return {"rows": rows, "affected": sorted(affected)}


_SXXEYY = re.compile(r"[Ss](\d{1,3})[ ._-]?[Ee](\d{1,4})(?:[ ._-]?[Ee-](\d{1,4}))?")
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".wmv", ".webm")


def import_folder(session: Session, folder: Path, title: str, tmdb_id: int | None = None) -> dict:
    """Register a folder of SxxEyy-named videos (or subtitle files) as a show, without Plex."""
    folder = folder.resolve()
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    show = session.scalar(select(Show).where(Show.local_path == str(folder)))
    if show is None:
        show = Show(title=title, local_path=str(folder))
        session.add(show)
    show.title = title
    if tmdb_id:
        show.tmdb_id = tmdb_id
    session.flush()

    found: dict[tuple[int, int], Path] = {}
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    for path in files:
        ext = path.suffix.lower()
        if ext not in VIDEO_EXTENSIONS and ext not in SUBTITLE_EXTENSIONS:
            continue
        match = _SXXEYY.search(path.name)
        if not match:
            continue
        season, first = int(match.group(1)), int(match.group(2))
        last = int(match.group(3)) if match.group(3) else first
        for number in range(first, max(first, last) + 1):
            key = (season, number)
            # Prefer the video; a bare subtitle file stands in when there is no video.
            if key not in found or ext in VIDEO_EXTENSIONS:
                found[key] = path
    for (season, number), path in sorted(found.items()):
        upsert_episode(session, show, season, number, file_path=str(path))
    link_multi_episode_files(session, show.id)
    return {"show_id": show.id, "episodes": len(found)}
