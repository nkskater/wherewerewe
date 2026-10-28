"""Show details for Up Next.

Plex's library listing gives every show's genres, cast, ratings, runtime and artwork in one
request per library. With a TMDB key, each show's details add keywords, creators, networks,
status and TMDB's related shows, refreshed every couple of weeks. Everything here is
show-level: the premise, never what happens later.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import get_state, set_state
from ..integrations.sonarr import SonarrClient
from ..models import Show, ShowMeta, TmdbTitle, WatchProgress
from ..sources.tmdb import TMDBClient
from .sync import plex_client

log = logging.getLogger(__name__)

TMDB_REFRESH_DAYS = 14
TMDB_BATCH = 250  # shows per job; the job queues itself again until every show is done
TMDB_PAUSE = 0.25  # seconds between TMDB requests, well inside its rate limit


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def sonarr_client(settings: Settings) -> SonarrClient:
    return SonarrClient(settings.sonarr_url, settings.sonarr_api_key, verify=settings.verify_ssl)


def _tags(m: dict, key: str, limit: int | None = None) -> list[str]:
    tags = [t["tag"] for t in m.get(key) or [] if t.get("tag")]
    return tags[:limit] if limit else tags


def _meta(session: Session, show_id: int) -> ShowMeta:
    meta = session.get(ShowMeta, show_id)
    if meta is None:
        meta = ShowMeta(show_id=show_id)
        session.add(meta)
    return meta


def apply_plex(meta: ShowMeta, m: dict) -> None:
    meta.summary = (m.get("summary") or "").strip() or meta.summary
    meta.tagline = m.get("tagline") or None
    meta.genres = _tags(m, "Genre")
    meta.cast = _tags(m, "Role", 8)
    meta.countries = _tags(m, "Country")
    meta.studio = m.get("studio") or None
    meta.content_rating = m.get("contentRating") or None
    meta.audience_rating = m.get("audienceRating")
    if m.get("duration"):
        meta.runtime_minutes = round(int(m["duration"]) / 60000)
    if m.get("addedAt"):
        meta.added_at = datetime.fromtimestamp(int(m["addedAt"]), UTC).replace(tzinfo=None)
    meta.art_url = m.get("art") or None
    blur = m.get("UltraBlurColors") or {}
    corners = ("topLeft", "topRight", "bottomRight", "bottomLeft")
    meta.colors = [f"#{blur[c]}" for c in corners if blur.get(c)]
    meta.plex_fetched_at = _now()


def sync_plex_meta(session: Session, settings: Settings) -> int:
    """Read every library show's details from Plex. Returns how many shows were updated."""
    by_key = {s.plex_rating_key: s for s in session.scalars(select(Show)) if s.plex_rating_key}
    wanted = {x.strip().lower() for x in settings.plex_libraries.split(",") if x.strip()}
    plex = plex_client(settings)
    count = 0
    try:
        try:
            set_state(session, "plex_machine_id", plex.identity())
        except Exception as exc:  # only needed for "Play in Plex" links
            log.warning("couldn't read the Plex server id: %s", exc)
        for section in plex.show_sections():
            if wanted and section["title"].lower() not in wanted:
                continue
            for m in plex.shows_raw(section["key"]):
                show = by_key.get(str(m.get("ratingKey")))
                if show is not None:
                    apply_plex(_meta(session, show.id), m)
                    count += 1
            session.commit()
    finally:
        plex.close()
    return count


def apply_tmdb(meta: ShowMeta, data: dict) -> None:
    meta.summary = meta.summary or data["overview"]
    meta.tmdb_status = data["status"]
    meta.language = data["language"]
    if data["genres"]:
        meta.genres = data["genres"]
    meta.keywords = data["keywords"][:40]
    meta.creators = data["creators"]
    meta.networks = data["networks"]
    meta.seasons_total = data["seasons"]
    meta.episodes_total = data["episodes"]
    meta.runtime_minutes = meta.runtime_minutes or data["runtime"]
    meta.vote_average = data["vote_average"]
    meta.vote_count = data["vote_count"]
    meta.tvdb_id = data["tvdb_id"]
    meta.related = data["related"]


def save_titles(session: Session, titles: list[dict], library: set[int]) -> None:
    for t in titles:
        if t["tmdb_id"] in library:
            continue
        row = session.get(TmdbTitle, t["tmdb_id"])
        if row is None:
            row = TmdbTitle(tmdb_id=t["tmdb_id"], name=t["name"])
            session.add(row)
        for key, value in t.items():
            setattr(row, key, value)
        row.fetched_at = _now()


def _due_for_tmdb(session: Session) -> list[Show]:
    cutoff = _now() - timedelta(days=TMDB_REFRESH_DAYS)
    fetched = {
        m.show_id: m.tmdb_fetched_at
        for m in session.scalars(select(ShowMeta))
        if m.tmdb_fetched_at is not None
    }
    watched = set(session.scalars(select(WatchProgress.show_id)))
    due = [s for s in session.scalars(select(Show)) if (fetched.get(s.id) or datetime.min) < cutoff]
    # Shows someone has watched first: they drive the recommendations.
    return sorted(due, key=lambda s: (s.id not in watched, fetched.get(s.id) or datetime.min))


def sync_tmdb_meta(
    session: Session, settings: Settings, limit: int = TMDB_BATCH, pause: float = TMDB_PAUSE
) -> tuple[int, int]:
    """Fetch TMDB details for up to `limit` shows. Returns (done, still due)."""
    if not settings.tmdb_api_key:
        return 0, 0
    due = _due_for_tmdb(session)
    library = {s.tmdb_id for s in session.scalars(select(Show)) if s.tmdb_id}
    client = TMDBClient(settings.tmdb_api_key)
    done = 0
    try:
        for show in due[:limit]:
            meta = _meta(session, show.id)
            try:
                if show.tmdb_id is None:
                    show.tmdb_id = client.find_show(show.title, show.year)
                data = client.show(show.tmdb_id) if show.tmdb_id else None
            except Exception as exc:  # TMDB down or rate limited: try again next run
                log.warning("TMDB details failed for %s: %s", show.title, exc)
                break
            if data:
                apply_tmdb(meta, data)
                save_titles(session, data["titles"], library)
            meta.tmdb_fetched_at = _now()  # also when TMDB doesn't know it, so it isn't retried
            done += 1
            if done % 25 == 0:
                session.commit()
            if pause:
                time.sleep(pause)
    finally:
        client.close()
    session.commit()
    return done, max(0, len(due) - done)


def plex_machine_id(session: Session) -> str | None:
    return get_state(session, "plex_machine_id")
