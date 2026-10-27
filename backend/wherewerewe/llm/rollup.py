"""Season rollups: one summary per completed season, built from its episode summaries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Episode, SeasonRollup
from ..services.library import current_summary, group_label, group_members
from . import prompts
from .client import LLM, estimate_tokens


def condensed_block(label: str, summary: dict, max_beats: int | None = None) -> str:
    beats = summary.get("plot_beats") or []
    if max_beats is not None:
        beats = beats[:max_beats]
    text = f"{label}: " + " ".join(b.strip() for b in beats)
    if max_beats is None and summary.get("cliffhanger"):
        text += f" Ends with: {summary['cliffhanger'].strip()}"
    return text


def season_episode_summaries(
    session: Session, show_id: int, season: int, model: str
) -> tuple[list[tuple[str, dict]], list[str]]:
    """(label, summary) for each file in a season, plus codes of episodes with no summary."""
    episodes = list(
        session.scalars(
            select(Episode)
            .where(
                Episode.show_id == show_id,
                Episode.season == season,
                Episode.part_of_episode_id.is_(None),
            )
            .order_by(Episode.episode)
        )
    )
    found, missing = [], []
    for ep in episodes:
        row = current_summary(session, ep.id, model, prompts.SUMMARY_VERSION)
        if row is None:
            missing.append(ep.code)
        else:
            found.append((group_label(group_members(session, ep)), row.summary_json))
    return found, missing


def current_rollup(session: Session, show_id: int, season: int, model: str) -> SeasonRollup | None:
    exact = session.scalar(
        select(SeasonRollup).where(
            SeasonRollup.show_id == show_id,
            SeasonRollup.season == season,
            SeasonRollup.model == model,
            SeasonRollup.prompt_version == prompts.ROLLUP_VERSION,
        )
    )
    if exact:
        return exact
    return session.scalar(
        select(SeasonRollup)
        .where(SeasonRollup.show_id == show_id, SeasonRollup.season == season)
        .order_by(SeasonRollup.created_at.desc(), SeasonRollup.id.desc())
        .limit(1)
    )


def build_rollup(session: Session, llm: LLM, show_id: int, season: int) -> SeasonRollup:
    found, missing = season_episode_summaries(session, show_id, season, llm.model)
    if missing:
        raise RuntimeError(f"season {season} is missing summaries: {', '.join(missing)}")
    if not found:
        raise RuntimeError(f"season {season} has no episodes")
    budget = llm.context_tokens - 1500
    max_beats = None
    while True:
        blocks = [condensed_block(label, s, max_beats) for label, s in found]
        if estimate_tokens("\n\n".join(blocks)) <= budget or max_beats == 2:
            break
        max_beats = 6 if max_beats is None else max(2, max_beats - 2)
    text = llm.chat(
        [
            {"role": "system", "content": prompts.ROLLUP_SYSTEM},
            {"role": "user", "content": prompts.rollup_user(season, blocks)},
        ],
        max_tokens=1200,
    ).strip()
    row = session.scalar(
        select(SeasonRollup).where(
            SeasonRollup.show_id == show_id,
            SeasonRollup.season == season,
            SeasonRollup.model == llm.model,
            SeasonRollup.prompt_version == prompts.ROLLUP_VERSION,
        )
    )
    if row is None:
        row = SeasonRollup(
            show_id=show_id,
            season=season,
            model=llm.model,
            prompt_version=prompts.ROLLUP_VERSION,
            episode_count=len(found),
            summary_text=text,
        )
        session.add(row)
    else:
        row.summary_text, row.episode_count = text, len(found)
    session.flush()
    return row
