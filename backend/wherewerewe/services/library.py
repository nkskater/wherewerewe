"""Episode ordering, multi-episode files, and lookups.

Ordering follows the (season, episode) numbers Plex reports, which already reflect
the show's ordering setting in Plex (aired, DVD, absolute). Season 0 (specials) has
no reliable position in that order, so specials never move a spoiler boundary and
are left out of recaps.
"""

from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Episode, EpisodeSummary, Show

_CODE = re.compile(r"^\s*s(\d{1,3})\s*e(\d{1,4})\s*$", re.IGNORECASE)


def parse_code(code: str) -> tuple[int, int]:
    match = _CODE.match(code)
    if not match:
        raise ValueError(f"expected an episode code like S02E04, got {code!r}")
    return int(match.group(1)), int(match.group(2))


def regular_episodes(session: Session, show_id: int) -> list[Episode]:
    """All non-special episodes of a show in order."""
    return list(
        session.scalars(
            select(Episode)
            .where(Episode.show_id == show_id, Episode.season > 0)
            .order_by(Episode.season, Episode.episode)
        )
    )


def group_members(session: Session, episode: Episode) -> list[Episode]:
    """The episodes stored in the same file as `episode` (just itself for normal files)."""
    primary_id = episode.part_of_episode_id or episode.id
    members = list(
        session.scalars(
            select(Episode)
            .where((Episode.id == primary_id) | (Episode.part_of_episode_id == primary_id))
            .order_by(Episode.season, Episode.episode)
        )
    )
    return members or [episode]


def group_label(members: list[Episode]) -> str:
    first, last = members[0], members[-1]
    if first.id == last.id:
        return first.code
    if first.season == last.season:
        return f"{first.code}-E{last.episode:02d}"
    return f"{first.code}-{last.code}"


def normalize_boundary(session: Session, episode: Episode) -> Episode:
    """Watching any part of a multi-episode file means the whole file was watched."""
    return group_members(session, episode)[-1]


def link_multi_episode_files(session: Session, show_id: int) -> None:
    """Point every later episode sharing a file at the first one in that file."""
    by_file: dict[str, list[Episode]] = defaultdict(list)
    episodes = list(session.scalars(select(Episode).where(Episode.show_id == show_id)))
    for ep in episodes:
        if ep.file_path:
            by_file[ep.file_path].append(ep)
    grouped: set[int] = set()
    for members in by_file.values():
        members.sort(key=lambda e: e.order_key)
        primary = members[0]
        primary.part_of_episode_id = None
        grouped.add(primary.id)
        for other in members[1:]:
            other.part_of_episode_id = primary.id
            grouped.add(other.id)
    for ep in episodes:
        if ep.id not in grouped:
            ep.part_of_episode_id = None


def find_show(session: Session, query: str) -> Show:
    """Find a show by id, exact title, or unique partial title."""
    if query.isdigit():
        show = session.get(Show, int(query))
        if show:
            return show
    exact = list(session.scalars(select(Show).where(func.lower(Show.title) == query.lower())))
    if len(exact) == 1:
        return exact[0]
    partial = list(
        session.scalars(select(Show).where(func.lower(Show.title).contains(query.lower())))
    )
    if len(partial) == 1:
        return partial[0]
    if not partial and not exact:
        raise LookupError(f"no show matches {query!r}")
    names = ", ".join(sorted(s.title for s in (exact or partial)))
    raise LookupError(f"{query!r} is ambiguous: {names}")


def episode_by_code(session: Session, show_id: int, code: str) -> Episode:
    season, number = parse_code(code)
    ep = session.scalar(
        select(Episode).where(
            Episode.show_id == show_id, Episode.season == season, Episode.episode == number
        )
    )
    if ep is None:
        raise LookupError(f"episode {code.upper()} not found")
    return ep


def current_summary(
    session: Session, episode_id: int, model: str, prompt_version: str
) -> EpisodeSummary | None:
    """The summary for the current model/prompt if there is one, else the newest of any."""
    exact = session.scalar(
        select(EpisodeSummary).where(
            EpisodeSummary.episode_id == episode_id,
            EpisodeSummary.model == model,
            EpisodeSummary.prompt_version == prompt_version,
        )
    )
    if exact:
        return exact
    return session.scalar(
        select(EpisodeSummary)
        .where(EpisodeSummary.episode_id == episode_id)
        .order_by(EpisodeSummary.created_at.desc(), EpisodeSummary.id.desc())
        .limit(1)
    )


def summarized_episode_ids(session: Session, show_id: int) -> set[int]:
    return set(
        session.scalars(
            select(EpisodeSummary.episode_id)
            .join(Episode, Episode.id == EpisodeSummary.episode_id)
            .where(Episode.show_id == show_id)
        )
    )
