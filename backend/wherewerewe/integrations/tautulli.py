"""Tautulli API v2: users and per-user episode watch history."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import httpx


class TautulliError(RuntimeError):
    pass


class TautulliClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        verify: bool = True,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30,
    ):
        if not url:
            raise TautulliError("TAUTULLI_URL is not set")
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=timeout,
            transport=transport or httpx.HTTPTransport(verify=verify),
        )
        self._api_key = api_key

    def _cmd(self, cmd: str, **params) -> object:
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._client.get(
                "/api/v2", params={"apikey": self._api_key, "cmd": cmd, **params}
            )
        except httpx.HTTPError as exc:
            raise TautulliError(f"Tautulli request failed: {exc}") from exc
        if resp.status_code == 401:
            raise TautulliError("Tautulli rejected the API key (401)")
        if resp.status_code != 200:
            raise TautulliError(f"Tautulli returned {resp.status_code} for {cmd}")
        body = resp.json().get("response", {})
        if body.get("result") != "success":
            raise TautulliError(f"Tautulli {cmd} failed: {body.get('message')}")
        return body.get("data")

    def check(self) -> str:
        info = self._cmd("get_tautulli_info") or {}
        return f"ok (Tautulli {info.get('tautulli_version', '?')})"

    def get_users(self) -> list[dict]:
        return list(self._cmd("get_users") or [])

    def iter_history(
        self, *, after: date | None = None, user_id: str | int | None = None, page_size: int = 500
    ) -> Iterator[dict]:
        """Episode history rows, newest first. Resumed sessions are grouped into one row."""
        start = 0
        while True:
            data = (
                self._cmd(
                    "get_history",
                    media_type="episode",
                    grouping=1,
                    order_column="date",
                    order_dir="desc",
                    start=start,
                    length=page_size,
                    after=after.isoformat() if after else None,
                    user_id=user_id,
                )
                or {}
            )
            rows = data.get("data") or []
            yield from rows
            start += len(rows)
            total = int(data.get("recordsFiltered") or 0)
            if not rows or start >= total:
                break

    def close(self) -> None:
        self._client.close()
