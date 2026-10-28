"""Indexing: dialogue source -> cached episode summary -> character ledger -> season rollup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..llm import prompts
from ..llm.client import LLM, get_llm
from ..llm.ledger import rebuild_ledger, update_ledger
from ..llm.rollup import season_episode_summaries
from ..llm.summarize import summarize_episode, synopsis_summary
from ..models import (
    Episode,
    EpisodeCast,
    EpisodeSource,
    EpisodeSummary,
    SeasonRollup,
    Show,
    WatchProgress,
)
from ..sources.resolver import NoSourceError, resolve_source
from ..sources.tmdb import TMDBClient, credited_in
from ..worker import queue
from .library import current_summary, group_label, group_members, regular_episodes

log = logging.getLogger(__name__)

AUTO_INDEX_ACTIVE_DAYS = 30
BACKFILL_ACTIVE_DAYS = 365


SYNOPSIS_MODEL = "synopsis"  # EpisodeSummary.model for summaries built without a model
SYNOPSIS_FORMAT = "synopsis-2"  # bump when synopsis_summary changes; they're rebuilt on start


def summary_model(settings: Settings) -> str:
    """The model key summaries are cached under for the current settings."""
    return SYNOPSIS_MODEL if settings.summary_source == "synopsis" else settings.llm_model


def _fill_tmdb(session: Session, settings: Settings, show: Show, members: list[Episode]) -> None:
    """Fetch each episode's TMDB overview and credited cast once."""
    if not settings.tmdb_api_key:
        return
    missing = [m for m in members if session.get(EpisodeCast, m.id) is None]
    if not missing:
        return
    try:
        client = TMDBClient(settings.tmdb_api_key)
        try:
            if show.tmdb_id is None:
                show.tmdb_id = client.find_show(show.title, show.year)
            if show.tmdb_id is None:
                # Not on TMDB: note that we looked, so these don't count as outdated forever.
                for m in missing:
                    session.add(EpisodeCast(episode_id=m.id, cast_json=[]))
                session.flush()
                return
            for m in missing:
                data = client.episode(show.tmdb_id, m.season, m.episode) or {}
                m.tmdb_overview = data.get("overview") or ""
                session.add(EpisodeCast(episode_id=m.id, cast_json=data.get("cast") or []))
            session.flush()
        finally:
            client.close()
    except Exception as exc:  # TMDB is supplementary; never fail indexing over it
        log.warning("TMDB lookup failed for %s: %s", show.title, exc)


def _without_cast(session: Session, episode_ids: list[int]) -> set[int]:
    if not episode_ids:
        return set()
    have = set(
        session.scalars(
            select(EpisodeCast.episode_id).where(EpisodeCast.episode_id.in_(episode_ids))
        )
    )
    return set(episode_ids) - have


def current_episode_ids(session: Session, settings: Settings, episode_ids: list[int]) -> set[int]:
    """Episodes whose summary is up to date: the current model and prompt, and, once there's
    a TMDB key, written with TMDB's cast list (so adding a key makes older ones outdated)."""
    if not episode_ids:
        return set()
    done = set(
        session.scalars(
            select(EpisodeSummary.episode_id).where(
                EpisodeSummary.episode_id.in_(episode_ids),
                EpisodeSummary.model == summary_model(settings),
                EpisodeSummary.prompt_version == prompts.SUMMARY_VERSION,
            )
        )
    )
    if settings.tmdb_api_key:
        done -= _without_cast(session, list(done))
    return done


def actors_as_of(session: Session, show_id: int, boundary: tuple[int, int]) -> dict[str, str]:
    """TMDB's character name (lowercased) -> actor, from episodes up to `boundary` only."""
    rows = session.execute(
        select(Episode, EpisodeCast)
        .join(EpisodeCast, EpisodeCast.episode_id == Episode.id)
        .where(Episode.show_id == show_id, Episode.season > 0)
    )
    actors: dict[str, str] = {}
    for ep, cast in rows:
        if ep.order_key > boundary:
            continue
        for person in cast.cast_json:
            if person.get("character") and person.get("actor"):
                actors.setdefault(person["character"].lower(), person["actor"])
    return actors


def played_by(entry, actors: dict[str, str]) -> str | None:
    """The actor for a ledger character: by full name or alias, else by a unique first name."""
    for name in [entry.name, *entry.aliases]:
        if name.lower() in actors:
            return actors[name.lower()]
    first = entry.name.split()[0].lower() if entry.name.split() else ""
    if len(first) < 3:
        return None
    hits = {a for c, a in actors.items() if c.split()[0] == first}
    return hits.pop() if len(hits) == 1 else None


def _cast(session: Session, members: list[Episode], text: str) -> list[dict]:
    people: dict[str, dict] = {}
    for m in members:
        row = session.get(EpisodeCast, m.id)
        for person in row.cast_json if row else []:
            people.setdefault(person["character"].lower(), person)
    return credited_in(list(people.values()), text)


def _source_path(settings: Settings, ep: Episode, source_type: str) -> Path:
    return settings.sources_dir / str(ep.show_id) / f"{ep.id}-{source_type}.txt"


def get_source_text(
    session: Session, settings: Settings, ep: Episode, members: list[Episode], force: bool = False
) -> tuple[str, str | None, str]:
    """Return (source_type, text, detail). text is None for overview-only episodes."""
    if not force:
        cached = session.scalar(
            select(EpisodeSource)
            .where(EpisodeSource.episode_id == ep.id, EpisodeSource.source_type != "overview")
            .order_by(EpisodeSource.quality_score.desc())
            .limit(1)
        )
        if cached and cached.text_path and Path(cached.text_path).exists():
            return cached.source_type, Path(cached.text_path).read_text(), cached.detail or ""
    durations = [m.duration_ms for m in members if m.duration_ms]
    duration = sum(durations) if len(durations) == len(members) else ep.duration_ms
    attempts: list[str] = []
    result = resolve_source(ep.file_path, duration, settings, attempts)
    path = _source_path(settings, ep, result.source_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.text)
    row = session.scalar(
        select(EpisodeSource).where(
            EpisodeSource.episode_id == ep.id, EpisodeSource.source_type == result.source_type
        )
    )
    if row is None:
        row = EpisodeSource(episode_id=ep.id, source_type=result.source_type)
        session.add(row)
    row.text_path, row.quality_score, row.detail = str(path), result.quality, result.detail
    session.flush()
    return result.source_type, result.text, result.detail


def index_episode(
    session: Session,
    settings: Settings,
    episode_id: int,
    *,
    force: bool = False,
    llm: LLM | None = None,
) -> str:
    ep = session.get(Episode, episode_id)
    if ep is None:
        raise LookupError(f"episode {episode_id} not found")
    if ep.season == 0:
        return f"{ep.code}: specials are not indexed"
    if ep.part_of_episode_id:
        return f"{ep.code}: summarized with the first episode in its file"
    model = summary_model(settings)
    members = group_members(session, ep)
    label = group_label(members)
    existing = session.scalar(
        select(EpisodeSummary).where(
            EpisodeSummary.episode_id == ep.id,
            EpisodeSummary.model == model,
            EpisodeSummary.prompt_version == prompts.SUMMARY_VERSION,
        )
    )
    # A summary written before the TMDB key was added is redone, now with the cast list.
    needs_cast = bool(settings.tmdb_api_key) and bool(
        _without_cast(session, [m.id for m in members])
    )
    if existing is not None and not force and not needs_cast:
        return f"{label}: already summarized"

    show = session.get(Show, ep.show_id)
    assert show is not None
    _fill_tmdb(session, settings, show, members)
    overview = "\n\n".join(m.overview for m in members if m.overview) or None

    if model == SYNOPSIS_MODEL:
        if not overview:
            raise NoSourceError("no synopsis from TMDB or Plex")
        summary = synopsis_summary(label, overview, _cast(session, members, overview))
        source_type, detail = "synopsis", "the synopsis"
    else:
        try:
            source_type, text, detail = get_source_text(session, settings, ep, members, force)
        except NoSourceError as exc:
            if not (overview and settings.allow_overview_only):
                raise
            log.info("%s %s: no dialogue (%s); using the synopsis", show.title, label, exc)
            source_type, text, detail = "overview", None, "synopsis only"
        cast = _cast(session, members, f"{text or ''}\n{overview or ''}")
        llm = llm or get_llm(settings)
        # Release the write lock before the slow model call, so a recap or another lane can
        # write meanwhile (SQLite allows one writer at a time).
        session.commit()
        summary = summarize_episode(llm, label, overview, text, cast).model_dump()
    if existing is None:
        existing = EpisodeSummary(
            episode_id=ep.id,
            model=model,
            prompt_version=prompts.SUMMARY_VERSION,
            source_type=source_type,
            summary_json=summary,
        )
        session.add(existing)
    else:
        existing.summary_json, existing.source_type = summary, source_type
        existing.created_at = datetime.now(UTC).replace(tzinfo=None)
    session.flush()
    update_ledger(session, ep, summary)
    maybe_enqueue_rollup(session, ep.show_id, ep.season, settings.llm_model)
    return f"{label}: summarized from {detail}"


def refresh_synopsis_summaries(session: Session) -> int:
    """Rebuild synopsis summaries from the stored synopses and cast. Returns how many."""
    rows = list(
        session.scalars(select(EpisodeSummary).where(EpisodeSummary.model == SYNOPSIS_MODEL))
    )
    for row in rows:
        ep = session.get(Episode, row.episode_id)
        if ep is None:
            continue
        members = group_members(session, ep)
        overview = "\n\n".join(m.overview for m in members if m.overview)
        if overview:
            cast = _cast(session, members, overview)
            row.summary_json = synopsis_summary(group_label(members), overview, cast)
    session.flush()
    return len(rows)


def rebuild_ledgers(session: Session, settings: Settings) -> int:
    """Rebuild every show's character ledger from its cached summaries. Returns shows done."""
    show_ids = list(
        session.scalars(
            select(Episode.show_id)
            .join(EpisodeSummary, EpisodeSummary.episode_id == Episode.id)
            .distinct()
        )
    )
    for show_id in show_ids:
        pairs = []
        for ep in regular_episodes(session, show_id):
            if ep.part_of_episode_id:
                continue
            row = current_summary(session, ep.id, summary_model(settings), prompts.SUMMARY_VERSION)
            if row is not None:
                pairs.append((ep, row.summary_json))
        rebuild_ledger(session, show_id, pairs)
        session.commit()
    return len(show_ids)


def maybe_enqueue_rollup(session: Session, show_id: int, season: int, model: str) -> None:
    if season == 0:
        return
    found, missing = season_episode_summaries(session, show_id, season, model)
    if missing or not found:
        return
    current = session.scalar(
        select(SeasonRollup).where(
            SeasonRollup.show_id == show_id,
            SeasonRollup.season == season,
            SeasonRollup.model == model,
            SeasonRollup.prompt_version == prompts.ROLLUP_VERSION,
        )
    )
    if current is not None and current.episode_count == len(found):
        return
    queue.enqueue(
        session,
        "rollup_season",
        {"show_id": show_id, "season": season},
        priority=1,
        dedupe_key=f"rollup_season:{show_id}:{season}",
    )


def max_boundary(session: Session, show_id: int) -> tuple[int, int] | None:
    rows = session.scalars(
        select(Episode)
        .join(WatchProgress, WatchProgress.last_watched_episode_id == Episode.id)
        .where(WatchProgress.show_id == show_id)
    )
    keys = [e.order_key for e in rows]
    return max(keys) if keys else None


def queue_show_index(
    session: Session,
    show_id: int,
    *,
    scope: str = "watched",
    force: bool = False,
    settings: Settings | None = None,
) -> int:
    """Queue index_episode jobs. scope="watched" stops at the furthest any user has watched.

    With `settings`, episodes that are already up to date are skipped."""
    limit = max_boundary(session, show_id) if scope == "watched" else None
    if scope == "watched" and limit is None:
        return 0
    episodes = [e for e in regular_episodes(session, show_id) if e.part_of_episode_id is None]
    done = set()
    if not force and settings is not None:
        done = current_episode_ids(session, settings, [e.id for e in episodes])
    queued = 0
    for ep in episodes:
        if limit is not None and ep.order_key > limit:
            continue
        if ep.id in done:
            continue
        # Index the latest watched episodes first; they matter most to a recap.
        priority = 2 if limit is not None and ep.season == limit[0] else 0
        queue.enqueue(
            session,
            "index_episode",
            {"episode_id": ep.id, "force": force},
            priority=priority,
            dedupe_key=f"index_episode:{ep.id}",
        )
        queued += 1
    return queued


def active_show_ids(session: Session, days: int) -> list[int]:
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    return list(
        session.scalars(
            select(WatchProgress.show_id).where(WatchProgress.last_watched_at >= since).distinct()
        )
    )


def show_stats(session: Session, show_id: int, settings: Settings | None = None) -> dict:
    episodes = [e for e in regular_episodes(session, show_id) if e.part_of_episode_id is None]
    ids = [e.id for e in episodes]
    sourced = summarized = 0
    if ids:
        sourced = (
            session.scalar(
                select(func.count(func.distinct(EpisodeSource.episode_id))).where(
                    EpisodeSource.episode_id.in_(ids)
                )
            )
            or 0
        )
        summarized = (
            session.scalar(
                select(func.count(func.distinct(EpisodeSummary.episode_id))).where(
                    EpisodeSummary.episode_id.in_(ids)
                )
            )
            or 0
        )
    rollups = (
        session.scalar(
            select(func.count(func.distinct(SeasonRollup.season))).where(
                SeasonRollup.show_id == show_id
            )
        )
        or 0
    )
    seasons = len({e.season for e in episodes})
    return {
        "episodes": len(episodes),
        "with_source": sourced,
        "summarized": summarized,
        # Summarized, but before a model, prompt or TMDB change; Index redoes these.
        "outdated": summarized - len(current_episode_ids(session, settings, ids))
        if settings is not None
        else 0,
        "seasons": seasons,
        "rollups": rollups,
    }
