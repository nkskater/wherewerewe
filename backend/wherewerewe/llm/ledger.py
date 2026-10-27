"""Character ledger: who each character is, as of a given point in the show.

States are recorded per episode. A lookup only ever reads states from episodes at
or before the viewer's boundary, including the names and aliases shown, because a
later episode can reveal a character's real name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Character, CharacterState, Episode

# Bump when matching or cleanup changes; the worker then rebuilds every ledger from the
# cached episode summaries (no model calls).
LEDGER_VERSION = "ledger-v2"

_HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "agent",
    "detective",
    "det",
    "officer",
    "sheriff",
    "captain",
    "capt",
    "lord",
    "lady",
    "ser",
    "sir",
    "king",
    "queen",
    "prince",
    "princess",
    "father",
    "uncle",
    "aunt",
    "judge",
    "professor",
    "prof",
    "sergeant",
    "sgt",
    "lt",
    "lieutenant",
    "general",
    "colonel",
    "deputy",
    "special",
    "pastor",
    "reverend",
    "rev",
    "coach",
    "nurse",
    "doctor",
}
_GENERIC = re.compile(r"^(a|an|the|unnamed|unknown|unidentified|various|several|some)\b", re.I)
# Words many people get called. As an alias they would merge unrelated characters.
_SHARED_NAMES = {
    "mom", "mother", "mama", "mommy", "momma", "ma", "mum", "dad", "father", "daddy",
    "papa", "pa", "pop", "pops", "grandma", "grandmother", "granny", "nana", "grandpa",
    "grandfather", "son", "daughter", "sister", "brother", "sis", "bro", "uncle", "aunt",
    "auntie", "cousin", "wife", "husband", "honey", "sweetie", "sweetheart", "babe", "baby",
    "darling", "dear", "boss", "sir", "ma'am", "madam", "buddy", "man", "dude", "kid", "boy",
    "girl", "pal", "friend", "partner", "doc", "chief", "pastor", "reverend", "coach",
    "officer", "detective", "agent", "sheriff", "captain", "teacher", "everyone", "guys",
}  # fmt: skip
_STATUSES = (
    ("dead", re.compile(r"\b(dead|died|dies|killed|deceased|murdered|executed)\b", re.I)),
    ("missing", re.compile(r"\b(missing|disappeared|kidnapped|abducted|captured)\b", re.I)),
    ("in custody", re.compile(r"\b(arrested|custody|jail(ed)?|prison|imprisoned)\b", re.I)),
    ("injured", re.compile(r"\b(injured|wounded|hospitali[sz]ed|in (the )?hospital)\b", re.I)),
    ("alive", re.compile(r"\b(alive|survives?|safe|fine)\b", re.I)),
)


def normalize_name(name: str) -> str:
    tokens = re.sub(r"[^\w\s'-]", " ", name.lower()).split()
    while len(tokens) > 1 and tokens[0].rstrip(".") in _HONORIFICS:
        tokens = tokens[1:]
    return " ".join(tokens)


def normalize_status(status: str | None) -> str | None:
    """Reduce free-text status to alive/dead/missing/in custody/injured; drop anything else.

    Models put jobs and relationship states ("teacher", "single") here otherwise.
    """
    for label, pattern in _STATUSES:
        if status and pattern.search(status):
            return label
    return None


def _is_name_like(alias: str) -> bool:
    """A real name or nickname, not a kinship word, description or possessive."""
    text = alias.strip()
    if not text or "'s " in text or text.lower().startswith(("the ", "my ", "his ", "her ")):
        return False
    if normalize_name(text) in _SHARED_NAMES or text.lower().strip(".") in _SHARED_NAMES:
        return False
    return any(ch.isupper() for ch in text)  # descriptions come back lowercase


def _keys(name: str, aliases: list[str]) -> set[str]:
    return {k for k in (normalize_name(n) for n in [name, *aliases]) if k}


def _candidates(characters: list[Character], word: str) -> list[Character]:
    """Characters a single word could refer to: as a whole name or a first or last name."""
    out = []
    for char in characters:
        for key in _keys(char.name, char.aliases or []):
            parts = key.split()
            if word == key or (len(parts) > 1 and word in (parts[0], parts[-1])):
                out.append(char)
                break
    return out


def match_character(characters: list[Character], name: str, aliases: list[str]) -> Character | None:
    """Find the existing character this name refers to.

    Multi-word names ("Frank Gaad") are specific enough to match exactly. A single word
    ("Gaad", "Agent Gaad", "Gemstone") matches only when exactly one character fits, so a
    shared surname or nickname never merges two people.
    """
    keys = _keys(name, aliases)
    for key in (k for k in keys if " " in k):
        for char in characters:
            if key in _keys(char.name, char.aliases or []):
                return char
    found: dict[int, Character] = {}
    for key in keys:
        parts = key.split()
        if len(parts) == 1:
            hits = _candidates(characters, key)
        else:  # "Judy Gemstone" vs a character so far only known as "Judy". First names
            # only: a lone surname ("Gemstone") is shared by the whole family.
            hits = [
                c
                for c in characters
                if any(" " not in k and k == parts[0] for k in _keys(c.name, c.aliases or []))
            ]
        if len(hits) == 1:
            found[hits[0].id] = hits[0]
    return next(iter(found.values())) if len(found) == 1 else None


def _is_ambiguous(characters: list[Character], name: str) -> bool:
    """A bare surname or nickname that fits several characters ("Dr. Gemstone")."""
    norm = normalize_name(name)
    return " " not in norm and len(_candidates(characters, norm)) > 1


def update_ledger(session: Session, episode: Episode, summary: dict) -> None:
    """Record character states from one episode summary (replacing any earlier run)."""
    session.execute(delete(CharacterState).where(CharacterState.episode_id == episode.id))
    characters = list(
        session.scalars(select(Character).where(Character.show_id == episode.show_id))
    )
    updates = [
        u
        for u in summary.get("characters") or []
        if (u.get("name") or "").strip() and not _GENERIC.match(u["name"].strip())
    ]
    # Names of everyone in this episode: one person's alias can't be another's name.
    episode_names = [normalize_name(u["name"]) for u in updates]
    seen: set[int] = set()
    for i, update in enumerate(updates):
        name = update["name"].strip()
        own = normalize_name(name)
        surname = own.split()[-1] if " " in own else None
        others = set()
        for j, other in enumerate(episode_names):
            if j != i:  # their full name and first name both belong to them
                others |= {other, other.split()[0]}
        aliases = []
        for alias in update.get("aliases") or []:
            alias = (alias or "").strip()
            norm = normalize_name(alias)
            # "Pastor Gemstone" reduces to the family surname, which fits every Gemstone.
            if norm in (own, surname) or norm in others or not _is_name_like(alias):
                continue
            aliases.append(alias)
        char = match_character(characters, name, aliases)
        if char is None:
            if _is_ambiguous(characters, name):
                continue
            char = Character(show_id=episode.show_id, name=name, aliases=aliases)
            session.add(char)
            session.flush()
            characters.append(char)
        else:
            taken = set().union(
                *(_keys(c.name, c.aliases or []) for c in characters if c.id != char.id)
            )
            known = _keys(char.name, char.aliases or [])
            extra = [
                a
                for a in [name, *aliases]
                if normalize_name(a) not in known and normalize_name(a) not in taken
            ]
            if extra:
                char.aliases = [*(char.aliases or []), *extra]
        if char.id in seen:
            continue
        seen.add(char.id)
        session.add(
            CharacterState(
                character_id=char.id,
                episode_id=episode.id,
                name_as_seen=name,
                aliases=aliases,
                role=update.get("role") or None,
                status=normalize_status(update.get("status")),
                new_info=update.get("new_info") or None,
                relationships_json=update.get("relationships") or [],
            )
        )
    session.flush()


def rebuild_ledger(session: Session, show_id: int, summaries: list[tuple[Episode, dict]]) -> None:
    """Rebuild a show's ledger from scratch from (episode, summary) pairs in episode order."""
    char_ids = select(Character.id).where(Character.show_id == show_id)
    session.execute(delete(CharacterState).where(CharacterState.character_id.in_(char_ids)))
    session.execute(delete(Character).where(Character.show_id == show_id))
    session.flush()
    for episode, summary in summaries:
        update_ledger(session, episode, summary)


@dataclass
class LedgerEntry:
    character_id: int
    name: str
    aliases: list[str]
    role: str | None
    status: str | None
    relationships: list[str]
    first_seen: str
    last_seen: str
    appearances: int
    history: list[dict] = field(default_factory=list)


def ledger_as_of(session: Session, show_id: int, boundary: tuple[int, int]) -> list[LedgerEntry]:
    """Every character seen at or before `boundary` (season, episode), as of that point."""
    rows = session.execute(
        select(CharacterState, Episode)
        .join(Episode, Episode.id == CharacterState.episode_id)
        .join(Character, Character.id == CharacterState.character_id)
        .where(Character.show_id == show_id, Episode.season > 0)
        .order_by(Episode.season, Episode.episode)
    ).all()
    by_char: dict[int, list[tuple[CharacterState, Episode]]] = {}
    for state, ep in rows:
        if (ep.season, ep.episode) <= boundary:
            by_char.setdefault(state.character_id, []).append((state, ep))

    entries = []
    for char_id, states in by_char.items():
        latest = states[-1][0]
        names: list[str] = []
        for state, _ in states:
            for n in [state.name_as_seen, *(state.aliases or [])]:
                if n and n not in names:
                    names.append(n)
        display = latest.name_as_seen
        role = next((s.role for s, _ in reversed(states) if s.role), None)
        status = next((s.status for s, _ in reversed(states) if s.status), None)
        relationships: list[str] = []
        for state, _ in states:
            for rel in state.relationships_json or []:
                if rel not in relationships:
                    relationships.append(rel)
        entries.append(
            LedgerEntry(
                character_id=char_id,
                name=display,
                aliases=[n for n in names if n != display],
                role=role,
                status=status,
                relationships=relationships[-8:],
                first_seen=states[0][1].code,
                last_seen=states[-1][1].code,
                appearances=len(states),
                history=_history(states),
            )
        )
    entries.sort(key=lambda e: (-e.appearances, e.name.lower()))
    return entries


def _history(states: list[tuple[CharacterState, Episode]]) -> list[dict]:
    """Per-episode notes; a status only shows where it changed ("dead" after "alive")."""
    out, previous = [], None
    for state, ep in states:
        changed = state.status if state.status and state.status != previous else None
        if state.status:
            previous = state.status
        if state.new_info or (changed and out):  # the first status is the starting point
            out.append({"episode": ep.code, "new_info": state.new_info, "status": changed})
    return out


def ledger_notes(entry: LedgerEntry) -> str:
    lines = [f"Name: {entry.name}"]
    if entry.aliases:
        lines.append(f"Also called: {', '.join(entry.aliases)}")
    if entry.role:
        lines.append(f"Role: {entry.role}")
    for item in entry.history:
        bits = [b for b in (item["new_info"], item["status"] and f"status: {item['status']}") if b]
        lines.append(f"{item['episode']}: {'; '.join(bits)}")
    if entry.relationships:
        lines.append("Relationships: " + "; ".join(entry.relationships))
    return "\n".join(lines)
