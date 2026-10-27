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
}
_GENERIC = re.compile(r"^(a|an|unnamed|unknown|unidentified|various|several|some)\b", re.IGNORECASE)


def normalize_name(name: str) -> str:
    tokens = re.sub(r"[^\w\s'-]", " ", name.lower()).split()
    while len(tokens) > 1 and tokens[0].rstrip(".") in _HONORIFICS:
        tokens = tokens[1:]
    return " ".join(tokens)


def _keys(name: str, aliases: list[str]) -> set[str]:
    return {k for k in (normalize_name(n) for n in [name, *aliases]) if k}


def match_character(characters: list[Character], name: str, aliases: list[str]) -> Character | None:
    keys = _keys(name, aliases)
    for char in characters:
        if keys & _keys(char.name, char.aliases or []):
            return char
    # "Gaad" vs "Frank Gaad": match a single token against the first or last token of a
    # multi-word name, but only when exactly one character fits.
    norm = normalize_name(name)
    candidates = []
    for char in characters:
        for key in _keys(char.name, char.aliases or []):
            a, b = key.split(), norm.split()
            if (len(b) == 1 and len(a) > 1 and b[0] in (a[0], a[-1])) or (
                len(a) == 1 and len(b) > 1 and a[0] in (b[0], b[-1])
            ):
                candidates.append(char)
                break
    return candidates[0] if len(candidates) == 1 else None


def update_ledger(session: Session, episode: Episode, summary: dict) -> None:
    """Record character states from one episode summary (replacing any earlier run)."""
    session.execute(delete(CharacterState).where(CharacterState.episode_id == episode.id))
    characters = list(
        session.scalars(select(Character).where(Character.show_id == episode.show_id))
    )
    seen: set[int] = set()
    for update in summary.get("characters") or []:
        name = (update.get("name") or "").strip()
        if not name or _GENERIC.match(name):
            continue
        aliases = [a.strip() for a in update.get("aliases") or [] if a and a.strip()]
        char = match_character(characters, name, aliases)
        if char is None:
            char = Character(show_id=episode.show_id, name=name, aliases=aliases)
            session.add(char)
            session.flush()
            characters.append(char)
        else:
            known = _keys(char.name, char.aliases or [])
            extra = [a for a in [name, *aliases] if normalize_name(a) not in known]
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
                status=update.get("status") or None,
                new_info=update.get("new_info") or None,
                relationships_json=update.get("relationships") or [],
            )
        )
    session.flush()


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
                history=[
                    {"episode": ep.code, "new_info": s.new_info, "status": s.status}
                    for s, ep in states
                    if s.new_info or s.status
                ],
            )
        )
    entries.sort(key=lambda e: (-e.appearances, e.name.lower()))
    return entries


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
