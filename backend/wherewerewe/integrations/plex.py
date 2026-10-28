"""Plex Media Server API: TV libraries, shows, episodes and file paths."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx


class PlexError(RuntimeError):
    pass


@dataclass
class PlexShow:
    rating_key: str
    title: str
    year: int | None
    thumb: str | None
    tmdb_id: int | None


@dataclass
class PlexEpisode:
    rating_key: str
    season: int
    episode: int
    title: str | None
    summary: str | None
    duration_ms: int | None
    air_date: str | None
    file_path: str | None


class PlexClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify: bool = True,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30,
    ):
        if not url:
            raise PlexError("PLEX_URL is not set")
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            headers={
                "X-Plex-Token": token,
                "Accept": "application/json",
                "X-Plex-Client-Identifier": "wherewerewe",
                "X-Plex-Product": "Where Were We",
            },
            timeout=timeout,
            transport=transport or httpx.HTTPTransport(verify=verify),
        )

    def _get(self, path: str, **params) -> dict:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PlexError(f"Plex request failed: {exc}") from exc
        if resp.status_code == 401:
            raise PlexError("Plex rejected the token (401)")
        if resp.status_code != 200:
            raise PlexError(f"Plex returned {resp.status_code} for {path}")
        return resp.json().get("MediaContainer", {})

    def identity(self) -> str:
        data = self._get("/identity")
        return data.get("machineIdentifier", "")

    def check(self) -> str:
        sections = self.show_sections()
        return f"ok ({len(sections)} TV libraries)"

    def show_sections(self) -> list[dict]:
        data = self._get("/library/sections")
        return [
            {"key": str(d["key"]), "title": d.get("title", "")}
            for d in data.get("Directory", [])
            if d.get("type") == "show"
        ]

    def shows(self, section_key: str) -> list[PlexShow]:
        data = self._get(f"/library/sections/{section_key}/all", type=2, includeGuids=1)
        return [self._parse_show(m) for m in data.get("Metadata", [])]

    def shows_raw(self, section_key: str) -> list[dict]:
        """The section's shows with everything Plex lists (genres, cast, ratings, art)."""
        data = self._get(f"/library/sections/{section_key}/all", type=2, includeGuids=1)
        return data.get("Metadata", [])

    def show(self, rating_key: str) -> PlexShow | None:
        data = self._get(f"/library/metadata/{rating_key}", includeGuids=1)
        items = data.get("Metadata", [])
        return self._parse_show(items[0]) if items else None

    def episodes(self, show_rating_key: str) -> list[PlexEpisode]:
        data = self._get(f"/library/metadata/{show_rating_key}/allLeaves")
        out = []
        for m in data.get("Metadata", []):
            if m.get("parentIndex") is None or m.get("index") is None:
                continue  # unmatched episodes have no reliable position
            file_path = None
            for media in m.get("Media", []):
                for part in media.get("Part", []):
                    if part.get("file"):
                        file_path = part["file"]
                        break
                if file_path:
                    break
            out.append(
                PlexEpisode(
                    rating_key=str(m["ratingKey"]),
                    season=int(m["parentIndex"]),
                    episode=int(m["index"]),
                    title=m.get("title"),
                    summary=m.get("summary") or None,
                    duration_ms=m.get("duration"),
                    air_date=m.get("originallyAvailableAt"),
                    file_path=file_path,
                )
            )
        return out

    def image(
        self, path: str, width: int | None = None, height: int | None = None
    ) -> tuple[bytes, str]:
        """Fetch artwork, resized by Plex's photo transcoder when a width is given.

        The height defaults to a poster's 2:3."""
        if width:
            try:
                return self._fetch_image(
                    "/photo/:/transcode",
                    {
                        "url": path,
                        "width": width,
                        "height": height or width * 3 // 2,
                        "minSize": 1,
                        "upscale": 1,
                    },
                )
            except PlexError:
                pass  # fall back to the original image
        return self._fetch_image(path, None)

    def _fetch_image(self, path: str, params: dict | None) -> tuple[bytes, str]:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PlexError(f"Plex image request failed: {exc}") from exc
        if resp.status_code != 200:
            raise PlexError(f"Plex returned {resp.status_code} for image")
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    @staticmethod
    def _parse_show(m: dict) -> PlexShow:
        tmdb_id = None
        for guid in m.get("Guid", []):
            match = re.match(r"tmdb://(\d+)", guid.get("id", ""))
            if match:
                tmdb_id = int(match.group(1))
        return PlexShow(
            rating_key=str(m["ratingKey"]),
            title=m.get("title", ""),
            year=m.get("year"),
            thumb=m.get("thumb"),
            tmdb_id=tmdb_id,
        )

    def close(self) -> None:
        self._client.close()
