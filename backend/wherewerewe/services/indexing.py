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
from ..llm.summarize import summarize_episode
from ..models import (
    Episode,
    EpisodeSource,
    EpisodeSummary,
    SeasonRollup,
    Show,
    WatchProgress,
)
from ..sources.resolver import NoSourceError, resolve_source
from ..sources.tmdb import TMDBClient
from ..worker import queue
from .library import current_summary, group_label, group_members, regular_episodes

log = logging.getLogger(__name__)

AUTO_INDEX_ACTIVE_DAYS = 30
BACKFILL_ACTIVE_DAYS = 365


def _fill_overviews(
    session: Session, settings: Settings, show: Show, members: list[Episode]
) -> None:
    if not settings.tmdb_api_key or not any(m.tmdb_overview is None for m in members):
        return
    try:
        client = TMDBClient(settings.tmdb_api_key)
        try:
            if show.tmdb_id is None:
                show.tmdb_id = client.find_show(show.title, show.year)
            if show.tmdb_id is None:
                return
            for m in members:
                if m.tmdb_overview is None:
                    m.tmdb_overview = (
                        client.episode_overview(show.tmdb_id, m.season, m.episode) or ""
                    )
        finally:
            client.close()
    except Exception as exc:  # TMDB is supplementary; never fail indexing over it
        log.warning("TMDB lookup failed for %s: %s", show.title, exc)


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
    llm = llm or get_llm(settings)
    members = group_members(session, ep)
    label = group_label(members)
    existing = session.scalar(
        select(EpisodeSummary).where(
            EpisodeSummary.episode_id == ep.id,
            EpisodeSummary.model == llm.model,
            EpisodeSummary.prompt_version == prompts.SUMMARY_VERSION,
        )
    )
    if existing is not None and not force:
        return f"{label}: already summarized"

    show = session.get(Show, ep.show_id)
    assert show is not None
    _fill_overviews(session, settings, show, members)
    overview = "\n\n".join(m.overview for m in members if m.overview) or None

    try:
        source_type, text, detail = get_source_text(session, settings, ep, members, force)
    except NoSourceError as exc:
        if not (overview and settings.allow_overview_only):
            raise
        log.info("%s %s: no dialogue (%s); summarizing from the synopsis", show.title, label, exc)
        source_type, text, detail = "overview", None, "synopsis only"

    # Release the write lock before the slow model call, so a recap or another lane can
    # write meanwhile (SQLite allows one writer at a time).
    session.commit()
    summary = summarize_episode(llm, label, overview, text).model_dump()
    if existing is None:
        existing = EpisodeSummary(
            episode_id=ep.id,
            model=llm.model,
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
    maybe_enqueue_rollup(session, ep.show_id, ep.season, llm.model)
    return f"{label}: summarized from {detail}"


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
            row = current_summary(session, ep.id, settings.llm_model, prompts.SUMMARY_VERSION)
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
    model: str | None = None,
) -> int:
    """Queue index_episode jobs. scope="watched" stops at the furthest any user has watched."""
    limit = max_boundary(session, show_id) if scope == "watched" else None
    if scope == "watched" and limit is None:
        return 0
    done = set()
    if not force and model:
        done = set(
            session.scalars(
                select(EpisodeSummary.episode_id)
                .join(Episode, Episode.id == EpisodeSummary.episode_id)
                .where(
                    Episode.show_id == show_id,
                    EpisodeSummary.model == model,
                    EpisodeSummary.prompt_version == prompts.SUMMARY_VERSION,
                )
            )
        )
    queued = 0
    episodes = [e for e in regular_episodes(session, show_id) if e.part_of_episode_id is None]
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


def show_stats(session: Session, show_id: int) -> dict:
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
        "seasons": seasons,
        "rollups": rollups,
    }
