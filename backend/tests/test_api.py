"""HTTP API, web pages, webhook gap detection and notifications."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import write_srt
from fastapi.testclient import TestClient
from sqlalchemy import select

from wherewerewe.api.app import create_app
from wherewerewe.db import load_settings, session_scope
from wherewerewe.models import Job, Recap, Show, User, WatchEvent
from wherewerewe.services.indexing import index_episode
from wherewerewe.services.library import episode_by_code, regular_episodes
from wherewerewe.services.progress import recompute_progress
from wherewerewe.services.sync import import_folder
from wherewerewe.worker.runner import run_pending


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def world(tmp_path, fake_llm):
    """A show with 4 episodes, all indexed, and a viewer who finished S01E03 `gap` days ago."""
    folder = tmp_path / "Show"
    folder.mkdir()
    for e in range(1, 5):
        write_srt(
            folder / f"Show.S01E{e:02d}.en.srt", [f"Walt: Episode {e} line.", f"Jesse: Reply {e}."]
        )
    with session_scope() as session:
        show_id = import_folder(session, folder, "The Show")["show_id"]
        show = session.get(Show, show_id)
        show.plex_rating_key = "500"
        eps = regular_episodes(session, show_id)
        for i, ep in enumerate(eps):
            ep.plex_rating_key = str(501 + i)
        settings = load_settings(session)
        for ep in eps:
            index_episode(session, settings, ep.id)
        user = User(plex_user_id="7", display_name="Alex")
        session.add(user)
        session.flush()
        for i, ep in enumerate(eps[:3]):
            session.add(
                WatchEvent(
                    user_id=user.id,
                    episode_id=ep.id,
                    percent_complete=100,
                    watched_at=now() - timedelta(days=30 - i),
                )
            )
        session.flush()
        recompute_progress(session, user.id, show_id, settings.watched_percent)
        ids = {"show": show_id, "user": user.id, "eps": [e.id for e in eps]}
    run_pending()  # season rollup
    return ids


def test_pages_render(client, world):
    for path in ["/", "/indexing", "/settings", f"/shows/{world['show']}/characters", "/recaps/1"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "Where Were We" in resp.text
    assert client.get("/static/app.js").status_code == 200


def test_users_and_shows(client, world):
    users = client.get("/api/users").json()
    assert users[0]["display_name"] == "Alex" and users[0]["effective_gap_days"] == 10
    shows = client.get(f"/api/users/{world['user']}/shows").json()
    assert shows[0]["last_watched_episode"] == "S01E03"
    assert shows[0]["next_episode"] == "S01E04"
    assert shows[0]["days_since"] == 28
    eps = client.get(f"/api/shows/{world['show']}/episodes?user={world['user']}").json()
    assert [e["code"] for e in eps] == ["S01E01", "S01E02", "S01E03"]
    listing = client.get("/api/shows").json()
    assert listing[0]["stats"]["summarized"] == 4 and listing[0]["stats"]["rollups"] == 1


def test_recap_flow_with_streaming(client, world, fake_llm):
    fake_llm.recap_text = "Walt and Jesse keep going. Jesse is worried."
    resp = client.post(
        "/api/recaps", json={"show_id": world["show"], "user_id": world["user"], "length": "quick"}
    )
    assert resp.status_code == 201
    recap = resp.json()
    assert recap["boundary"] == "S01E03" and recap["status"] == "queued"
    run_pending()
    with client.stream("GET", f"/api/recaps/{recap['id']}?stream=true") as stream:
        body = "".join(stream.iter_text())
    assert "event: final" in body and "Jesse is worried." in body
    done = client.get(f"/api/recaps/{recap['id']}").json()
    assert done["status"] == "done" and done["text"] == fake_llm.recap_text
    assert done["verified"] is True
    # The recap prompt held nothing past the boundary and no title.
    recap_prompt = next(c for c in fake_llm.calls if c["kind"] == "recap")["messages"][1]
    assert "S01E04" not in recap_prompt["content"]
    assert "The Show" not in recap_prompt["content"]
    # Same request again is served from cache unless fresh is asked for.
    again = client.post(
        "/api/recaps", json={"show_id": world["show"], "user_id": world["user"], "length": "quick"}
    )
    assert again.json()["id"] == recap["id"]
    fresh = client.post(
        "/api/recaps",
        json={"show_id": world["show"], "user_id": world["user"], "length": "quick", "fresh": True},
    )
    assert fresh.json()["id"] != recap["id"]


def test_recap_refuses_to_go_past_the_viewer(client, world):
    resp = client.post(
        "/api/recaps", json={"show_id": world["show"], "user_id": world["user"], "until": "S01E04"}
    )
    assert resp.status_code == 400 and "past where this user has watched" in resp.text
    ok = client.post(
        "/api/recaps", json={"show_id": world["show"], "user_id": world["user"], "until": "S01E02"}
    )
    assert ok.status_code == 201 and ok.json()["boundary"] == "S01E02"
    chars = client.get(f"/api/shows/{world['show']}/characters?user={world['user']}&until=S01E04")
    assert chars.status_code == 403


def test_characters_and_whois(client, world):
    data = client.get(f"/api/shows/{world['show']}/characters?user={world['user']}").json()
    assert data["as_of"] == "S01E03"
    names = {c["name"]: c for c in data["characters"]}
    assert names["Walt"]["last_seen"] == "S01E03"  # S01E04 is past the boundary
    answer = client.post(
        f"/api/shows/{world['show']}/whois",
        json={"character_id": names["Walt"]["character_id"], "user": world["user"]},
    ).json()
    assert answer["answer"] == "They are someone you have met."
    filtered = client.get(f"/api/shows/{world['show']}/characters?user={world['user']}&q=jes")
    assert [c["name"] for c in filtered.json()["characters"]] == ["Jesse"]


def test_settings_roundtrip_masks_secrets(client):
    resp = client.put(
        "/api/settings",
        json={
            "plex_url": "https://192.168.1.100:32400",
            "plex_token": "secret",
            "verify_ssl": "false",
            "default_gap_days": "14",
        },
    )
    assert resp.status_code == 200
    fields = {f["key"]: f for g in resp.json()["groups"] for f in g["fields"]}
    assert fields["plex_url"]["value"] == "https://192.168.1.100:32400"
    assert fields["plex_token"]["value"] == "********"
    assert fields["verify_ssl"]["value"] is False
    assert fields["default_gap_days"]["value"] == 14 and fields["default_gap_days"]["overridden"]
    # Masked secret round-trips unchanged; null clears an override.
    client.put("/api/settings", json={"plex_token": "********", "default_gap_days": None})
    with session_scope() as session:
        s = load_settings(session)
        assert s.plex_token == "secret" and s.default_gap_days == 10
    bad = client.put("/api/settings", json={"default_gap_days": "soon", "nope": "x"})
    assert bad.status_code == 422


def test_health_reports_unconfigured_services(client, fake_llm):
    data = client.get("/api/health").json()
    assert data["checks"]["database"]["ok"]
    assert data["checks"]["plex"] == {"ok": False, "detail": "not configured"}
    assert data["checks"]["llm"]["ok"]
    assert client.get("/api/health/live").json() == {"ok": True}


def _play(client, rating_key="504", user_id="7", action="play", **extra):
    return client.post(
        "/webhooks/tautulli",
        json={
            "action": action,
            "media_type": "episode",
            "user_id": user_id,
            "rating_key": rating_key,
            "grandparent_rating_key": "500",
            **extra,
        },
    ).json()


def test_webhook_gap_triggers_recap_and_notification(client, world, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "wherewerewe.integrations.notifiers.send",
        lambda target, title, message, url, settings: (
            sent.append((target, title, message, url)) or "ntfy"
        ),
    )
    client.put(f"/api/users/{world['user']}", json={"notify_target": "https://ntfy.sh/tv"})
    result = _play(client)
    assert result["result"] == "recap_queued" and result["gap_days"] == 28
    run_pending()
    with session_scope() as session:
        recap = session.get(Recap, result["recap_id"])
        assert recap.status == "done" and recap.length == "quick" and recap.trigger == "webhook"
        assert episode_by_code(session, world["show"], "S01E03").id == recap.boundary_episode_id
    assert sent and sent[0][0] == "https://ntfy.sh/tv"
    assert sent[0][1] == "Previously on The Show"
    assert sent[0][3].endswith(f"/recaps/{result['recap_id']}")
    # Pausing and resuming must not send a second notification.
    assert _play(client)["result"] == "already_sent"


def test_webhook_rewatch_recaps_only_what_came_before(client, world):
    result = _play(client, rating_key="502")  # replaying S01E02
    with session_scope() as session:
        recap = session.get(Recap, result["recap_id"])
        assert recap.boundary_episode_id == episode_by_code(session, world["show"], "S01E01").id


def test_webhook_without_gap_or_unknown(client, world):
    client.put(f"/api/users/{world['user']}", json={"gap_threshold_days": 60})
    assert _play(client)["result"] == "no_gap"
    assert _play(client, user_id="999")["result"] == "unknown"
    assert _play(client, action="pause")["result"] == "ignored"
    stop = _play(client, action="stop")
    assert stop["result"] == "sync_queued"
    with session_scope() as session:
        job = session.get(Job, stop["job_id"])
        assert job.type == "sync_history" and job.payload == {"plex_user_id": "7"}
    movie = client.post("/webhooks/tautulli", json={"action": "play", "media_type": "movie"})
    assert movie.json()["result"] == "ignored"


def test_webhook_secret(client, world, monkeypatch):
    client.put("/api/settings", json={"webhook_secret": "s3"})
    assert client.post("/webhooks/tautulli", json={"action": "play"}).status_code == 401
    ok = client.post("/webhooks/tautulli?token=s3", json={"action": "pause"})
    assert ok.status_code == 200


def test_jobs_and_index_endpoints(client, world):
    resp = client.post(f"/api/shows/{world['show']}/index", json={"scope": "all", "force": True})
    job_id = resp.json()["job_id"]
    run_pending()
    jobs = client.get("/api/jobs").json()
    assert jobs["counts"]["done"] >= 1
    assert any(j["id"] == job_id and j["status"] == "done" for j in jobs["jobs"])
    with session_scope() as session:
        job = session.scalar(select(Job).where(Job.type == "index_episode"))
        job.status = "failed"
        failed_id = job.id
    assert client.post(f"/api/jobs/{failed_id}/retry").json()["status"] == "queued"
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409
