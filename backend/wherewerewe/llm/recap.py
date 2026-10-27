"""Hierarchical recap assembly.

A recap only ever receives summaries of episodes at or before the viewer's
boundary. That structural guarantee is the first and strongest spoiler defence;
the grounding prompt, title omission and verification pass sit on top of it.

Within the context budget:
- earlier seasons are represented by their season rollups,
- the current season by its episode summaries, with the last few in full detail,
- open threads from the latest episodes are carried forward explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Episode, Show
from ..services.library import (
    current_summary,
    group_label,
    normalize_boundary,
    regular_episodes,
)
from . import prompts
from .client import LLM, estimate_tokens
from .rollup import condensed_block, current_rollup

LENGTHS = ("quick", "standard", "deep")


@dataclass
class RecapContext:
    text: str
    boundary: Episode
    boundary_label: str
    coverage: dict = field(default_factory=dict)


def _detailed_block(label: str, s: dict) -> str:
    lines = [f"### {label}", "What happens:"]
    lines += [f"- {b.strip()}" for b in s.get("plot_beats") or []]
    chars = [c for c in s.get("characters") or [] if c.get("name")]
    if chars:
        lines.append("Characters:")
        for c in chars:
            bits = [x for x in (c.get("new_info"), c.get("status") and f"now: {c['status']}") if x]
            lines.append(f"- {c['name']}" + (f": {'; '.join(bits)}" if bits else ""))
    if s.get("relationships_changed"):
        lines.append("Relationships changed: " + "; ".join(s["relationships_changed"]))
    if s.get("resolved_threads"):
        lines.append("Resolved: " + "; ".join(s["resolved_threads"]))
    if s.get("cliffhanger"):
        lines.append(f"Ends with: {s['cliffhanger'].strip()}")
    return "\n".join(lines)


def build_context(
    session: Session,
    show: Show,
    boundary_episode: Episode,
    length: str,
    settings: Settings,
    context_tokens: int,
) -> RecapContext:
    if length not in LENGTHS:
        raise ValueError(f"length must be one of {', '.join(LENGTHS)}")
    if boundary_episode.season == 0:
        raise ValueError("specials (season 0) can't be used as a recap boundary")
    boundary = normalize_boundary(session, boundary_episode)
    bkey = boundary.order_key
    watched = [e for e in regular_episodes(session, show.id) if e.order_key <= bkey]

    groups: dict[int, list[Episode]] = {}
    for ep in watched:
        groups.setdefault(ep.part_of_episode_id or ep.id, []).append(ep)

    model = settings.llm_model
    by_season: dict[int, list[tuple[str, dict]]] = {}
    missing: list[str] = []
    overview_only: list[str] = []
    for primary_id, members in groups.items():
        label = group_label(members)
        row = current_summary(session, primary_id, model, prompts.SUMMARY_VERSION)
        if row is None:
            missing.append(label)
            continue
        if row.source_type == "overview":
            overview_only.append(label)
        by_season.setdefault(members[0].season, []).append((label, row.summary_json))

    current_season = bkey[0]
    earlier = sorted(s for s in by_season if s < current_season)
    rollups: dict[int, str] = {}
    for season in earlier:
        rollup = current_rollup(session, show.id, season, model)
        if rollup is not None:
            rollups[season] = rollup.summary_text.strip()

    current_eps = by_season.get(current_season, [])
    k = max(1, settings.recap_detail_episodes)
    recent = current_eps[-k:]
    older_current = current_eps[:-k] if len(current_eps) > k else []
    if not current_eps and earlier:
        # Nothing summarized in the current season yet: detail the last season we have.
        last = earlier[-1]
        recent = by_season[last][-k:]

    threads: list[str] = []
    for _label, s in reversed(recent):
        for t in s.get("open_threads") or []:
            if t not in threads:
                threads.append(t)
    threads = threads[:8]
    final_moment = recent[-1][1].get("cliffhanger") if recent else None

    reserve = prompts.LENGTH_MAX_TOKENS[length] + 1200
    budget = context_tokens - reserve
    # Degrade detail step by step until the context fits.
    level = 0
    dropped_seasons: list[int] = []
    while True:
        sections: list[str] = []
        if not settings.recap_hide_title:
            sections.append(f"Series: {show.title}")
        kept_earlier = [s for s in earlier if s not in dropped_seasons]
        if dropped_seasons:
            sections.append(
                f"(Seasons {dropped_seasons[0]}-{dropped_seasons[-1]} are omitted "
                "for length; focus on the later material.)"
            )
        for season in kept_earlier:
            if season in rollups:
                text = rollups[season]
                if level >= 2:
                    text = text[:1200].rsplit(" ", 1)[0] + "..."
                sections.append(f"## Season {season} (season summary)\n{text}")
            else:
                beats = 3 if level >= 1 else None
                blocks = [condensed_block(lbl, s, beats) for lbl, s in by_season[season]]
                sections.append(f"## Season {season} (episode by episode)\n" + "\n".join(blocks))
        recent_labels = {lbl for lbl, _ in recent}
        if current_eps:
            current_older = [(lbl, s) for lbl, s in older_current if lbl not in recent_labels]
            if current_older:
                beats = 3 if level >= 1 else None
                sections.append(
                    f"## Season {current_season}, earlier episodes\n"
                    + "\n".join(condensed_block(lbl, s, beats) for lbl, s in current_older)
                )
        detail = recent if level < 4 else recent[-1:]
        if level >= 4 and len(recent) > 1:
            sections.append(
                "## Other recent episodes\n"
                + "\n".join(condensed_block(lbl, s, 3) for lbl, s in recent[:-1])
            )
        recent_season = current_season if current_eps else (earlier[-1] if earlier else 0)
        sections.append(
            f"## Season {recent_season}, most recent episodes (in detail)\n"
            + "\n\n".join(_detailed_block(lbl, s) for lbl, s in detail)
        )
        if threads:
            sections.append(
                "## Unresolved as of the viewer's last episode\n"
                + "\n".join(f"- {t}" for t in threads)
            )
        if final_moment:
            sections.append(f"## The last thing the viewer saw\n{final_moment}")
        text = "\n\n".join(sections)
        if estimate_tokens(text) <= budget:
            break
        if level < 4:
            level += 1
        elif len(kept_earlier) > 1:
            dropped_seasons.append(kept_earlier[0])
        else:
            break  # as small as it gets; let the model cope

    coverage = {
        "watched": len(groups),
        "summarized": len(groups) - len(missing),
        "missing": missing,
        "overview_only": overview_only,
        "rollup_seasons": sorted(rollups),
        "seasons_without_rollup": [s for s in earlier if s not in rollups],
        "dropped_seasons": dropped_seasons,
        "detail_level": level,
    }
    return RecapContext(
        text=text, boundary=boundary, boundary_label=boundary.code, coverage=coverage
    )


def write_recap(
    llm: LLM,
    context: str,
    length: str,
    on_text: Callable[[str], None] | None = None,
    extra_instruction: str = "",
) -> str:
    user = prompts.recap_user(context, length)
    if extra_instruction:
        user += "\n\n" + extra_instruction
    text = ""
    for piece in llm.stream(
        [{"role": "system", "content": prompts.RECAP_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=prompts.LENGTH_MAX_TOKENS[length],
    ):
        text += piece
        if on_text:
            on_text(text)
    return text.strip()
