"""TMDB episode overviews, used as supplementary grounding for watched episodes only."""

from __future__ import annotations

import httpx

API = "https://api.themoviedb.org/3"


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

    def episode_overview(self, tv_id: int, season: int, episode: int) -> str | None:
        resp = self._client.get(f"/tv/{tv_id}/season/{season}/episode/{episode}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return (resp.json().get("overview") or "").strip() or None

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
