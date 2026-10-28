"""TMDB: episode overviews and credits for recaps, and show details for Up Next.

Episode credits are fetched only for episodes someone has watched, so a character's name is
never known before an episode they're credited in. Show details are show-level (premise,
genres, keywords, related shows) and never describe later episodes.
"""

from __future__ import annotations

import re

import httpx

API = "https://api.themoviedb.org/3"
IMAGES = "https://image.tmdb.org/t/p"
# TMDB's TV genre ids (stable), for related shows that only carry ids.
TV_GENRES = {
    10759: "Action & Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    10762: "Kids",
    9648: "Mystery",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
    37: "Western",
}
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


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _title(item: dict) -> dict:
    return {
        "tmdb_id": int(item["id"]),
        "name": item.get("name") or item.get("original_name") or "",
        "year": _year(item.get("first_air_date")),
        "overview": (item.get("overview") or "").strip() or None,
        "poster_path": item.get("poster_path"),
        "backdrop_path": item.get("backdrop_path"),
        "genres": [TV_GENRES[g] for g in item.get("genre_ids") or [] if g in TV_GENRES],
        "language": item.get("original_language"),
        "vote_average": item.get("vote_average"),
        "vote_count": item.get("vote_count"),
    }


def parse_show(data: dict) -> dict:
    related, titles, seen = [], [], set()
    for kind in ("recommendations", "similar"):
        for rank, item in enumerate((data.get(kind) or {}).get("results") or []):
            if not item.get("id") or (item.get("adult")):
                continue
            related.append(
                {
                    "tmdb_id": int(item["id"]),
                    "rank": rank,
                    "kind": "rec" if kind[0] == "r" else "similar",
                }
            )
            if item["id"] not in seen:
                seen.add(item["id"])
                titles.append(_title(item))
    runtimes = [r for r in data.get("episode_run_time") or [] if r]
    return {
        "name": data.get("name") or "",
        "overview": (data.get("overview") or "").strip() or None,
        "status": data.get("status"),
        "language": data.get("original_language"),
        "genres": [g["name"] for g in data.get("genres") or [] if g.get("name")],
        "keywords": [k["name"] for k in (data.get("keywords") or {}).get("results") or []],
        "creators": [c["name"] for c in data.get("created_by") or [] if c.get("name")],
        "networks": [n["name"] for n in data.get("networks") or [] if n.get("name")],
        "seasons": data.get("number_of_seasons"),
        "episodes": data.get("number_of_episodes"),
        "runtime": min(runtimes) if runtimes else None,
        "vote_average": data.get("vote_average"),
        "vote_count": data.get("vote_count"),
        "tvdb_id": (data.get("external_ids") or {}).get("tvdb_id"),
        "related": related,
        "titles": titles,
    }


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

    def show(self, tv_id: int) -> dict | None:
        """Show details with keywords, related shows and external ids, or None if unknown."""
        resp = self._client.get(
            f"/tv/{tv_id}",
            params={"append_to_response": "keywords,recommendations,similar,external_ids"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return parse_show(resp.json())

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
