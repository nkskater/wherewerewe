"""TMDB episode overviews and credits, fetched only for episodes someone has watched.

Credits are per episode, so a character's name is never known before an episode they're
credited in.
"""

from __future__ import annotations

import re

import httpx

API = "https://api.themoviedb.org/3"
_NOT_CHARACTERS = re.compile(
    r"^(self|himself|herself|themselves|narrator|host|various|unknown|uncredited)\b", re.I
)


def clean_character(raw: str) -> str:
    """'Walter White (voice)' -> 'Walter White'; 'Himself' -> ''."""
    text = re.sub(r"\s*\((voice|uncredited|archive footage|credit only)\)", "", raw, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return "" if _NOT_CHARACTERS.match(text) else text


def credited_in(cast: list[dict], text: str) -> list[dict]:
    """Guest stars, plus regulars whose first or last name occurs in this episode's text.

    TMDB lists a season's regulars on every episode, including ones introduced later, so a
    regular only counts once the episode itself mentions them.
    """
    return [p for p in cast if p["guest"] or mentions(text, p["character"])]


def mentions(text: str, character: str) -> bool:
    """Whether `text` names this character by first or last name."""
    tokens = set(re.findall(r"[a-z][a-z'-]+", text.lower()))
    names = re.split(r"\s*/\s*", character)
    words = {w.lower() for n in names for w in (n.split()[:1] + n.split()[-1:]) if len(w) > 2}
    # "Walt" in the dialogue counts for "Walter White".
    return any(t == w or (len(t) >= 4 and w.startswith(t)) for w in words for t in tokens)


class TMDBClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        # v4 read access tokens are JWTs; v3 keys are 32 hex characters.
        headers, params = {}, {}
        if api_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            params["api_key"] = api_key
        self._client = httpx.Client(
            base_url=API, headers=headers, params=params, timeout=20, transport=transport
        )

    def episode(self, tv_id: int, season: int, episode: int) -> dict | None:
        """{"overview": str | None, "cast": [{"character", "actor", "guest"}]} or None."""
        resp = self._client.get(
            f"/tv/{tv_id}/season/{season}/episode/{episode}",
            params={"append_to_response": "credits"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        credits = data.get("credits") or {}
        cast = []
        seen = set()
        for guest, people in ((False, credits.get("cast")), (True, credits.get("guest_stars"))):
            for person in people or []:
                character = clean_character(person.get("character") or "")
                if character and character.lower() not in seen:
                    seen.add(character.lower())
                    cast.append(
                        {"character": character, "actor": person.get("name"), "guest": guest}
                    )
        return {"overview": (data.get("overview") or "").strip() or None, "cast": cast}

    def episode_overview(self, tv_id: int, season: int, episode: int) -> str | None:
        data = self.episode(tv_id, season, episode)
        return data["overview"] if data else None

    def find_show(self, title: str, year: int | None = None) -> int | None:
        params: dict = {"query": title}
        if year:
            params["first_air_date_year"] = year
        resp = self._client.get("/search/tv", params=params)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return int(results[0]["id"]) if results else None

    def close(self) -> None:
        self._client.close()
