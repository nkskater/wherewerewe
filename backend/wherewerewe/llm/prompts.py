"""Prompts and output schemas.

Bump a *_VERSION constant whenever its prompt or schema changes; cached rows
carry the version so stale ones can be regenerated selectively.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

SUMMARY_VERSION = "summary-v2"
ROLLUP_VERSION = "rollup-v1"
RECAP_VERSION = "recap-v1"
VERIFY_VERSION = "verify-v1"
WHOIS_VERSION = "whois-v1"

GROUNDING = (
    "Use ONLY the material provided in this conversation. Do not use any outside knowledge "
    "about this series, its characters, or its actors, even if you think you recognize it. "
    "Never mention, hint at, or foreshadow anything that is not in the provided material."
)


class CharacterUpdate(BaseModel):
    name: str = Field(description="Their fullest name as used in this episode")
    aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Other names THIS SAME person goes by here: first name, surname, nickname. "
            "Never kinship or pet words (Mom, Daddy, honey), never another character's name, "
            "never a description. Usually empty"
        ),
    )
    role: str = Field(
        default="", description="Who they are in the story, e.g. 'FBI agent, Stan's boss'"
    )
    # Plain str so unconstrained endpoints still parse; the enum constrains decoding where
    # supported, and the ledger normalizes whatever comes back.
    status: str = Field(
        default="alive",
        description="Physical state at the end of the episode; jobs and relationships go in role",
        json_schema_extra={"enum": ["alive", "dead", "missing", "in custody", "injured"]},
    )
    new_info: str = Field(default="", description="What we learned about them in this episode")
    relationships: list[str] = Field(
        default_factory=list, description="Relationships shown, e.g. 'married to Elizabeth'"
    )


class EpisodeSummaryOut(BaseModel):
    episode: str
    plot_beats: list[str] = Field(
        default_factory=list, description="5-12 chronological plot points"
    )
    characters: list[CharacterUpdate] = Field(default_factory=list)
    relationships_changed: list[str] = Field(default_factory=list)
    open_threads: list[str] = Field(
        default_factory=list, description="Unresolved questions at the end of the episode"
    )
    resolved_threads: list[str] = Field(default_factory=list)
    cliffhanger: str = Field(default="", description="The final situation the episode ends on")


SUMMARY_SYSTEM = f"""You summarize a single episode of a television series from its dialogue \
transcript and official synopsis. {GROUNDING}

Subtitles rarely say who is speaking. Infer speakers from context (names used in \
conversation, who is being addressed). When you cannot tell, describe the person neutrally \
("a man", "the agent") rather than guessing a name. Bracketed lines like [gunshot] are sound \
cues. Lines prefixed "Name:" are labelled speakers.

Write plot beats in chronological order as short factual sentences in present tense. In \
"characters", include every named character who appears, speaks, or is significantly \
discussed, describing their role in the story (never as actors or "guest stars"). List each \
person once, under their fullest name. Family members are separate people: a parent called \
"Daddy" and a son are two entries, and neither gets the other's name as an alias. List \
unresolved questions in "open_threads" and end with the "cliffhanger" the episode closes on. \
Respond with JSON matching the schema."""


def summary_user(
    code: str,
    overview: str | None,
    transcript: str | None,
    notes: str | None = None,
    cast: list[dict] | None = None,
) -> str:
    parts = [f"Episode: {code}"]
    if overview:
        parts.append(f"Official synopsis:\n{overview}")
    if cast:
        names = ", ".join(c["character"] + (" (guest)" if c.get("guest") else "") for c in cast)
        parts.append(
            "Characters credited in this episode. Use these spellings, and list only those who "
            f"actually appear or are discussed:\n{names}"
        )
    if notes:
        parts.append(f"Notes on the episode's dialogue, in order:\n{notes}")
    elif transcript:
        parts.append(f"Dialogue transcript:\n{transcript}")
    else:
        parts.append(
            "No transcript is available; base the summary on the synopsis alone and keep it short."
        )
    parts.append(f'Write the JSON summary for {code}. Set "episode" to "{code}".')
    return "\n\n".join(parts)


CHUNK_SYSTEM = f"""You take notes on part of a TV episode's dialogue transcript. {GROUNDING}
Write concise chronological notes: who is involved, what happens, what is revealed. \
Infer speakers from context; don't guess names you can't support. Plain text, no preamble."""


def chunk_user(index: int, total: int, text: str) -> str:
    return f"Part {index} of {total} of the transcript:\n\n{text}\n\nNotes:"


ROLLUP_SYSTEM = f"""You condense episode summaries of one season of a television series into a \
season summary. {GROUNDING}

Write 250-450 words of plain prose in present tense covering the season's main storylines in \
order, then a final paragraph beginning "By the end of the season," describing where each \
major character stands and what remains unresolved. No headings, no lists."""


def rollup_user(season: int, episode_blocks: list[str]) -> str:
    return (
        f"Episode summaries for season {season}:\n\n"
        + "\n\n".join(episode_blocks)
        + f"\n\nWrite the season {season} summary."
    )


RECAP_SYSTEM = f"""You write the "Previously on..." recap a viewer reads right before their next \
episode of a television series. {GROUNDING}

The viewer has seen everything in the source material and nothing after it. Rules:
- Every statement must come from the source material. Do not add names, events, motives or \
details that are not there.
- Do not predict or speculate about what happens next. Present unresolved threads as open \
questions, not hints.
- Do not name the series.
- Spend the most words on the most recent episodes; that is what the viewer needs most.
- Present tense, plain and direct, no filler like "in this thrilling season"."""

LENGTH_INSTRUCTIONS = {
    "quick": "Write ONE paragraph of 4-6 sentences focused on the latest episodes and what is "
    "unresolved going into the next one. No headings.",
    "standard": "Write about 300-450 words: a short paragraph on the story so far, then the "
    "most recent episodes in more detail, ending with the open questions going into the next "
    "episode. No headings.",
    "deep": "Write a season-by-season recap. Start each season with a markdown heading like "
    "'## Season 1' and write a paragraph or two for it, giving the latest season the most "
    "detail. Finish with a '## Where things stand' section of 3-6 short bullet points.",
}
LENGTH_MAX_TOKENS = {"quick": 400, "standard": 1100, "deep": 2600}


def recap_user(context: str, length: str) -> str:
    return (
        f"SOURCE MATERIAL\n\n{context}\n\nEND OF SOURCE MATERIAL\n\n{LENGTH_INSTRUCTIONS[length]}"
    )


class SentenceVerdict(BaseModel):
    id: int
    supported: bool
    reason: str = ""


class VerifyOut(BaseModel):
    results: list[SentenceVerdict] = Field(default_factory=list)


VERIFY_SYSTEM = """You are a strict fact checker. You receive SOURCE MATERIAL and numbered \
SENTENCES from a recap written from it. For each sentence decide if it is supported: every \
name, event and claim in it must be stated in, or directly implied by, the source material. \
Mark a sentence unsupported if it adds a name, event, outcome, motive or detail that the source \
material does not contain, or if it predicts or hints at future events. Framing an unresolved \
thread as an open question is fine. Use only the source material, never outside knowledge. \
Return one result per sentence id."""


def verify_user(context: str, sentences: list[tuple[int, str]], hints: dict[int, str]) -> str:
    lines = []
    for sid, sentence in sentences:
        hint = f"  (note: {hints[sid]})" if sid in hints else ""
        lines.append(f"[{sid}] {sentence}{hint}")
    return f"SOURCE MATERIAL\n\n{context}\n\nEND OF SOURCE MATERIAL\n\nSENTENCES\n\n" + "\n".join(
        lines
    )


WHOIS_SYSTEM = f"""You answer a TV viewer's question about a character, using only what the \
viewer has seen so far, given as notes from each episode up to their current position. \
{GROUNDING} Answer in 2-5 sentences. If the notes don't answer the question, say what is known \
and that the rest hasn't been shown yet."""


def whois_user(question: str, notes: str) -> str:
    return f"Character notes, oldest first:\n\n{notes}\n\nQuestion: {question}"


PITCH_SYSTEM = """You help one person decide what to watch next. Write 2-3 sentences, \
to them, on why they might enjoy this show, connecting it to shows they liked. Use only the \
premise given: no twists, deaths, reveals, or anything past the setup. Don't invent facts \
about the show, its cast, or awards. No lists, no headings, no preamble, and don't start with \
the show's title."""


def pitch_user(title: str, premise: str, details: str, liked: list[str], reasons: list[str]) -> str:
    return (
        f"Show: {title}\n{details}\nPremise: {premise}\n\n"
        f"Shows they finished or loved: {', '.join(liked) or 'none yet'}\n"
        f"Why it was picked for them: {'; '.join(reasons) or 'a good fit for the library'}"
    )
