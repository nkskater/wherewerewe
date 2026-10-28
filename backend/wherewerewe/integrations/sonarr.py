"""Sonarr: see which shows it already has, and add one an admin picked from Up Next.

The only write is `add`, and only when someone presses "Add to Sonarr".
"""

from __future__ import annotations

import contextlib

import httpx


class SonarrError(RuntimeError):
    pass


class SonarrClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify: bool = True,
        transport: httpx.BaseTransport | None = None,
    ):
        if not url or not api_key:
            raise SonarrError("Sonarr isn't set up")
        self._client = httpx.Client(
            base_url=url.rstrip("/") + "/api/v3",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=30,
            transport=transport or httpx.HTTPTransport(verify=verify),
        )

    def _request(self, method: str, path: str, **kw):
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise SonarrError(f"couldn't reach Sonarr: {exc}") from exc
        if resp.status_code == 401:
            raise SonarrError("Sonarr rejected the API key")
        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                body = resp.json()
                if isinstance(body, list) and body and isinstance(body[0], dict):
                    detail = body[0].get("errorMessage") or detail
                elif isinstance(body, dict):
                    detail = body.get("message") or detail
            except ValueError:
                pass
            raise SonarrError(f"Sonarr returned {resp.status_code}: {detail}")
        return resp.json() if resp.content else None

    def check(self) -> str:
        status = self._request("GET", "/system/status")
        return f"ok (Sonarr {status.get('version', '?')})"

    def series_ids(self) -> tuple[set[int], set[int]]:
        """(tmdb ids, tvdb ids) of every series Sonarr has."""
        tmdb, tvdb = set(), set()
        for s in self._request("GET", "/series") or []:
            if s.get("tmdbId"):
                tmdb.add(int(s["tmdbId"]))
            if s.get("tvdbId"):
                tvdb.add(int(s["tvdbId"]))
        return tmdb, tvdb

    def _pick(self, path: str, wanted: str, key: str) -> dict:
        items = self._request("GET", path) or []
        if not items:
            raise SonarrError(f"Sonarr has no {path.strip('/')} set up")
        if wanted:
            for item in items:
                if (
                    str(item.get(key, "")).lower() == wanted.lower()
                    or str(item.get("id")) == wanted
                ):
                    return item
            raise SonarrError(f"Sonarr has no {path.strip('/')} called {wanted!r}")
        return items[0]

    def add(
        self, tvdb_id: int, *, quality_profile: str = "", root_folder: str = "", search: bool = True
    ) -> dict:
        found = self._request("GET", "/series/lookup", params={"term": f"tvdb:{tvdb_id}"}) or []
        if not found:
            raise SonarrError("Sonarr couldn't find that show")
        series = found[0]
        if series.get("id"):
            return {"title": series.get("title"), "added": False}
        body = {
            **series,
            "qualityProfileId": self._pick("/qualityprofile", quality_profile, "name")["id"],
            "rootFolderPath": self._pick("/rootfolder", root_folder, "path")["path"],
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {"monitor": "all", "searchForMissingEpisodes": search},
        }
        # Sonarr v3 also needs a language profile; v4 dropped them.
        with contextlib.suppress(SonarrError, IndexError, KeyError, TypeError):
            body["languageProfileId"] = self._request("GET", "/languageprofile")[0]["id"]
        added = self._request("POST", "/series", json=body)
        return {"title": added.get("title") if added else series.get("title"), "added": True}

    def close(self) -> None:
        self._client.close()
