"""Login: off until an admin password is set; Plex sign-in for owners and viewers."""

import pytest
from fastapi.testclient import TestClient
from test_api import world  # noqa: F401  (fixture)

from wherewerewe import auth
from wherewerewe.api import auth_routes
from wherewerewe.api.app import create_app
from wherewerewe.db import session_scope
from wherewerewe.integrations.plextv import PlexAccount, ServerAccess
from wherewerewe.models import Recap, Setting, User
from wherewerewe.services.library import regular_episodes


@pytest.fixture
def client():
    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


class FakePlexTV:
    account_id = "7"
    access = ServerAccess(allowed=True, owner=False)
    seen_tokens: list[str] = []  # noqa: RUF012

    def __init__(self, client_id):
        self.client_id = client_id

    def create_pin(self):
        return 42, "pin-code"

    def auth_url(self, code, forward):
        return f"https://app.plex.tv/auth#?code={code}&forwardUrl={forward}"

    def pin_token(self, pin_id, code):
        assert (pin_id, code) == (42, "pin-code")
        return "user-token"

    def account(self, token):
        return PlexAccount(id=FakePlexTV.account_id, username="someone", title="Someone")

    def server_access(self, token, machine_id):
        assert machine_id == "machine-1"
        return FakePlexTV.access

    def close(self):
        pass


class FakePlex:
    def identity(self):
        return "machine-1"

    def close(self):
        pass


@pytest.fixture
def plex_login(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setattr(auth_routes.plextv, "PlexTV", FakePlexTV)
    monkeypatch.setattr(auth_routes, "plex_client", lambda settings: FakePlex())
    FakePlexTV.account_id = "7"
    FakePlexTV.access = ServerAccess(allowed=True, owner=False)


def sign_in_with_plex(client) -> object:
    start = client.post("/auth/plex/start", json={"next": "/"})
    assert start.status_code == 200 and "app.plex.tv" in start.json()["url"]
    assert "forwardUrl=http://testserver/auth/plex/callback" in start.json()["url"]
    return client.get("/auth/plex/callback")


def test_passwords_are_hashed_and_checked():
    stored = auth.hash_password("s3cret")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert auth.verify_password(stored, "s3cret")
    assert not auth.verify_password(stored, "wrong")
    assert auth.verify_password("plain-from-env", "plain-from-env")
    assert not auth.verify_password("", "")


def test_everything_is_open_without_an_admin_password(client):
    assert client.get("/api/settings").status_code == 200
    assert client.get("/library").status_code == 200
    assert client.get("/login").headers["location"] == "/"
    assert client.get("/api/me").json()["login"] is False


def test_password_login(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    assert client.get("/api/settings").status_code == 401
    page = client.get("/library?x=1")
    assert page.status_code == 303 and page.headers["location"] == "/login?next=/library%3Fx%3D1"
    # Still reachable without signing in:
    assert client.get("/login").status_code == 200
    assert client.get("/api/health/live").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.post("/webhooks/tautulli", json={"action": "pause"}).status_code == 200

    assert client.post("/auth/password", json={"password": "nope"}).status_code == 401
    ok = client.post("/auth/password", json={"password": "correct-horse", "next": "//evil.example"})
    assert ok.status_code == 200 and ok.json()["next"] == "/"
    cookie = ok.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/settings").status_code == 200
    assert client.get("/library").status_code == 200
    assert client.get("/api/me").json() == {
        "login": True,
        "name": "Admin",
        "admin": True,
        "user_id": None,
        "method": "password",
    }
    client.post("/auth/logout")
    assert client.get("/api/settings").status_code == 401


def test_changing_the_password_signs_everyone_out(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    client.post("/auth/password", json={"password": "correct-horse"})
    assert client.put("/api/settings", json={"admin_password": "new-one"}).status_code == 200
    with session_scope() as session:
        stored = session.get(Setting, "admin_password")
        assert stored.value.startswith("scrypt$")  # never stored in plain text
    assert client.get("/api/settings").status_code == 401
    assert client.post("/auth/password", json={"password": "correct-horse"}).status_code == 401
    assert client.post("/auth/password", json={"password": "new-one"}).status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_tampered_session_is_rejected(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    client.post("/auth/password", json={"password": "correct-horse"})
    token = client.cookies.get(auth.COOKIE)
    body, sig = token.rsplit(".", 1)
    client.cookies.set(auth.COOKIE, f"{body}.{'0' * len(sig)}")
    assert client.get("/api/settings").status_code == 401


def test_plex_viewer_only_sees_their_own(client, world, plex_login):  # noqa: F811
    done = sign_in_with_plex(client)
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert auth.PIN_COOKIE not in client.cookies
    me = client.get("/api/me").json()
    assert me["admin"] is False and me["method"] == "plex"

    with session_scope() as session:
        alex = session.query(User).filter_by(plex_user_id="7").one()
        other = User(plex_user_id="8", display_name="Sam")
        session.add(other)
        session.flush()
        other_recap = Recap(
            user_id=other.id,
            show_id=world["show"],
            boundary_episode_id=regular_episodes(session, world["show"])[0].id,
            length="quick",
            status="done",
        )
        session.add(other_recap)
        session.flush()
        alex_id, other_id, other_recap_id = alex.id, other.id, other_recap.id
    assert me["user_id"] == alex_id

    assert [u["display_name"] for u in client.get("/api/users").json()] == ["Alex"]
    assert client.get(f"/api/users/{alex_id}/shows").status_code == 200
    assert client.get(f"/api/users/{other_id}/shows").status_code == 403
    assert client.put(f"/api/users/{other_id}", json={"notify_target": "x"}).status_code == 403
    assert client.get(f"/api/recaps/{other_recap_id}").status_code == 404
    # Admin-only areas
    for path in ("/api/settings", "/api/jobs", "/api/shows", "/api/health", "/api/recaps"):
        assert client.get(path).status_code == 403, path
    assert client.post("/api/sync", json={}).status_code == 403
    assert client.get("/library").headers["location"] == "/"
    assert client.get("/docs").headers["location"] == "/"
    # Their own things work, and they always act as themselves.
    assert client.get("/").status_code == 200
    assert client.get(f"/shows/{world['show']}/characters").status_code == 200
    chars = client.get(f"/api/shows/{world['show']}/characters")
    assert chars.status_code == 200 and chars.json()["as_of"] == "S01E03"
    assert client.get(f"/api/shows/{world['show']}/characters?user={other_id}").status_code == 403
    recap = client.post("/api/recaps", json={"show_id": world["show"], "length": "quick"})
    assert recap.status_code == 201 and recap.json()["user_id"] == alex_id
    assert client.get(f"/api/recaps/{recap.json()['id']}").status_code == 200
    # ...and can't ask for a recap past where they are.
    past = client.post(
        "/api/recaps", json={"show_id": world["show"], "length": "quick", "until": "S01E04"}
    )
    assert past.status_code == 400


def test_plex_owner_is_admin(client, world, plex_login):  # noqa: F811
    FakePlexTV.account_id = "99"
    FakePlexTV.access = ServerAccess(allowed=True, owner=True)
    assert sign_in_with_plex(client).status_code == 303
    assert client.get("/api/me").json()["admin"] is True
    assert client.get("/api/settings").status_code == 200
    with session_scope() as session:
        assert session.query(User).filter_by(plex_user_id="99").one().display_name == "Someone"


def test_plex_account_without_access_is_turned_away(client, plex_login):
    FakePlexTV.access = ServerAccess(allowed=False, owner=False)
    done = sign_in_with_plex(client)
    assert done.headers["location"].startswith("/login?error=")
    assert client.get("/api/me").status_code == 401


def test_plex_callback_needs_the_pin_from_this_browser(client, plex_login):
    done = client.get("/auth/plex/callback")
    assert done.headers["location"].startswith("/login?error=")


def test_plex_sign_in_can_be_turned_off(client, plex_login, monkeypatch):
    monkeypatch.setenv("PLEX_LOGIN", "false")
    assert client.post("/auth/plex/start", json={}).status_code == 400
    assert "Sign in with Plex" not in client.get("/login").text
