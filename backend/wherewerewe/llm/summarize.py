"""Per-episode structured summaries (generated once, cached, shared across users).

Each episode is summarized in isolation from its own dialogue and synopsis, so a
summary can never contain anything from a later episode. Spoiler safety then comes
from which summaries a recap is assembled from.
"""

from __future__ import annotations

import logging
import re

from . import prompts
from .client import LLM, chat_json, estimate_tokens

log = logging.getLogger(__name__)

SUMMARY_MAX_TOKENS = 3000
_PROMPT_OVERHEAD = 700


def _split_lines(text: str, max_tokens: int) -> list[str]:
    chunks, current, size = [], [], 0
    for line in text.splitlines():
        cost = estimate_tokens(line) + 1
        if current and size + cost > max_tokens:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


def synopsis_summary(code: str, overview: str, cast: list[dict]) -> dict:
    """An episode summary built from the synopsis and credits alone, with no model call."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", overview) if s.strip()]
    characters = []
    for person in cast:
        name, *aliases = [n.strip() for n in person["character"].split("/") if n.strip()]
        characters.append(
            {
                "name": name,
                "aliases": aliases,
                "role": f"Played by {person['actor']}" if person.get("actor") else "",
                "status": "",
                "new_info": "",
                "relationships": [],
            }
        )
    return prompts.EpisodeSummaryOut(
        episode=code,
        plot_beats=sentences,
        characters=[prompts.CharacterUpdate(**c) for c in characters],
        cliffhanger=sentences[-1] if sentences else "",
    ).model_dump()


def summarize_episode(
    llm: LLM,
    code: str,
    overview: str | None,
    transcript: str | None,
    cast: list[dict] | None = None,
) -> prompts.EpisodeSummaryOut:
    budget = (
        llm.context_tokens - SUMMARY_MAX_TOKENS - _PROMPT_OVERHEAD - estimate_tokens(overview or "")
    )
    notes = None
    if transcript and estimate_tokens(transcript) > budget:
        # Too long for one pass: take notes per chunk, then summarize the notes.
        chunk_budget = max(1000, llm.context_tokens - 1500 - _PROMPT_OVERHEAD)
        chunks = _split_lines(transcript, chunk_budget)
        log.info("%s: transcript too long for context, summarizing in %d parts", code, len(chunks))
        parts = []
        for i, chunk in enumerate(chunks, 1):
            reply = llm.chat(
                [
                    {"role": "system", "content": prompts.CHUNK_SYSTEM},
                    {"role": "user", "content": prompts.chunk_user(i, len(chunks), chunk)},
                ],
                max_tokens=1200,
            )
            parts.append(f"Part {i}:\n{reply.strip()}")
        notes = "\n\n".join(parts)
    result = chat_json(
        llm,
        [
            {"role": "system", "content": prompts.SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": prompts.summary_user(code, overview, transcript, notes, cast),
            },
        ],
        prompts.EpisodeSummaryOut,
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    result.episode = code
    return result
