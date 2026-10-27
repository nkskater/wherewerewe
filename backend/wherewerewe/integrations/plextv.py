"""Sign in with Plex: the plex.tv PIN flow.

1. Create a PIN, send the browser to app.plex.tv with its code.
2. Plex sends the browser back to us; the PIN now carries the user's token.
3. The token tells us who they are and which servers they can use. We keep neither the
   token nor anything else from plex.tv beyond their account id and name.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

PLEX_TV = "https://plex.tv"
AUTH_APP = "https://app.plex.tv/auth#?"
PRODUCT = "Where Were We"


class PlexTVError(RuntimeError):
    pass


@dataclass
class PlexAccount:
    id: str
    username: str
    title: str


@dataclass
class ServerAccess:
    allowed: bool
    owner: bool


class PlexTV:
    def __init__(self, client_id: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=PLEX_TV,
            headers={
                "Accept": "application/json",
                "X-Plex-Product": PRODUCT,
                "X-Plex-Client-Identifier": client_id,
            },
            timeout=15,
            transport=transport,
        )
        self.client_id = client_id

    def _json(self, method: str, path: str, token: str | None = None, **kw) -> dict | list:
        headers = {"X-Plex-Token": token} if token else {}
        try:
            resp = self._client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise PlexTVError(f"couldn't reach plex.tv: {exc}") from exc
        if resp.status_code == 401:
            raise PlexTVError("plex.tv rejected the sign-in")
        if resp.status_code >= 400:
            raise PlexTVError(f"plex.tv returned {resp.status_code}")
        return resp.json()

    def create_pin(self) -> tuple[int, str]:
        data = self._json("POST", "/api/v2/pins", params={"strong": "true"})
        assert isinstance(data, dict)
        return int(data["id"]), str(data["code"])

    def auth_url(self, code: str, forward_url: str) -> str:
        return AUTH_APP + urlencode(
            {
                "clientID": self.client_id,
                "code": code,
                "forwardUrl": forward_url,
                "context[device][product]": PRODUCT,
            }
        )

    def pin_token(self, pin_id: int, code: str) -> str | None:
        data = self._json("GET", f"/api/v2/pins/{pin_id}", params={"code": code})
        assert isinstance(data, dict)
        return data.get("authToken") or None

    def account(self, token: str) -> PlexAccount:
        data = self._json("GET", "/api/v2/user", token=token)
        assert isinstance(data, dict)
        return PlexAccount(
            id=str(data["id"]),
            username=data.get("username") or "",
            title=data.get("title") or data.get("username") or "",
        )

    def server_access(self, token: str, machine_id: str) -> ServerAccess:
        """Whether this account can use the server with this machine identifier."""
        data = self._json("GET", "/api/v2/resources", token=token, params={"includeHttps": 1})
        for res in data if isinstance(data, list) else []:
            if res.get("clientIdentifier") == machine_id:
                return ServerAccess(allowed=True, owner=bool(res.get("owned")))
        return ServerAccess(allowed=False, owner=False)

    def close(self) -> None:
        self._client.close()
