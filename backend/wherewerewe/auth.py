"""Login: an admin password, and Sign in with Plex for everyone with access to the server.

Login is on once ADMIN_PASSWORD is set. Sessions are signed cookies; the signing key is
generated on first use and kept in the database.

- Admin (the password, or the Plex account that owns the server): everything.
- Viewer (any other Plex account with access to the server): their own shows, recaps
  and characters, nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .config import Settings
from .db import get_state, set_state

COOKIE = "www_session"
PIN_COOKIE = "www_plex_pin"
SESSION_SECONDS = 30 * 24 * 3600
PIN_SECONDS = 15 * 60


@dataclass(frozen=True)
class Principal:
    name: str
    admin: bool
    user_id: int | None = None  # the viewer this person is; None for the password admin
    method: str = "open"  # open (login off), password, plex


OPEN = Principal(name="", admin=True)


def login_enabled(settings: Settings) -> bool:
    return bool(settings.admin_password)


# ---- passwords ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(stored: str, given: str) -> bool:
    """`stored` is a hash from the Settings page or a plain ADMIN_PASSWORD from the env."""
    if not stored or not given:
        return False
    if stored.startswith("scrypt$"):
        _, salt, digest = stored.split("$", 2)
        candidate = hashlib.scrypt(
            given.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(candidate.hex(), digest)
    return hmac.compare_digest(stored.encode(), given.encode())


def password_version(stored: str) -> str:
    """Changes whenever the password does, so old password sessions stop working."""
    return hashlib.sha256(stored.encode()).hexdigest()[:12]


# ---- signed values -----------------------------------------------------------------------


def _key(session: Session) -> bytes:
    key = get_state(session, "auth_key")
    if not key:
        key = secrets.token_hex(32)
        set_state(session, "auth_key", key)
        session.commit()
    return bytes.fromhex(key)


def client_id(session: Session) -> str:
    """This install's identifier towards plex.tv."""
    value = get_state(session, "plex_client_id")
    if not value:
        value = f"wherewerewe-{secrets.token_hex(8)}"
        set_state(session, "plex_client_id", value)
        session.commit()
    return value


def sign(session: Session, payload: dict, ttl: int) -> str:
    body = {**payload, "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    sig = hmac.new(_key(session), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def unsign(session: Session, token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(_key(session), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("exp", 0) < time.time():
        return None
    return body


def session_token(session: Session, principal: Principal, settings: Settings) -> str:
    payload = {
        "name": principal.name,
        "admin": principal.admin,
        "user_id": principal.user_id,
        "method": principal.method,
    }
    if principal.method == "password":
        payload["pv"] = password_version(settings.admin_password)
    return sign(session, payload, SESSION_SECONDS)


def principal_from_token(
    session: Session, token: str | None, settings: Settings
) -> Principal | None:
    body = unsign(session, token)
    if body is None:
        return None
    if body.get("method") == "password" and body.get("pv") != password_version(
        settings.admin_password
    ):
        return None
    if body.get("method") == "plex" and not settings.plex_login:
        return None
    return Principal(
        name=str(body.get("name") or ""),
        admin=bool(body.get("admin")),
        user_id=body.get("user_id"),
        method=str(body.get("method")),
    )


# ---- what needs which role ---------------------------------------------------------------

PUBLIC = re.compile(r"^/(login|auth/|static/|webhooks/|favicon\.ico$|api/health/live$)")
# Everything a viewer may reach; routes still check it's their own data.
VIEWER = re.compile(
    r"^/($"
    r"|upnext$"
    r"|recaps/\d+$"
    r"|shows/\d+/characters$"
    r"|api/me$"
    r"|api/users$"
    r"|api/users/\d+(/shows|/upnext|/upnext/item|/preferences|/pitch)?$"
    r"|api/shows/\d+(/poster|/art|/characters|/whois)?$"
    r"|api/tmdb/image/w\d+/[\w.-]+$"
    r"|api/recaps(/\d+)?$)"
)


def is_public(path: str) -> bool:
    return bool(PUBLIC.match(path))


def viewer_may_open(method: str, path: str) -> bool:
    if not VIEWER.match(path):
        return False
    # Viewers can start recaps and ask "who is", but not list everyone's recaps.
    return not (path == "/api/recaps" and method == "GET")
