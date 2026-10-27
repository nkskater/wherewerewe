"""Plex library scan and Tautulli history sync against mocked APIs."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from wherewerewe.db import load_settings, session_scope
from wherewerewe.integrations.plex import PlexClient
from wherewerewe.integrations.tautulli import TautulliClient
from wherewerewe.models import Episode, Show, User, WatchEvent, WatchProgress
from wherewerewe.services import sync
from wherewerewe.services.progress import boundary_episode, shows_in_progress


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def ts(days_ago: float) -> int:
    return int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp())


def plex_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-Plex-Token"] == "tok"
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [
                        {"key": "2", "type": "show", "title": "TV Shows"},
                        {"key": "1", "type": "movie", "title": "Movies"},
                    ]
                }
            },
        )
    if path == "/library/sections/2/all":
        assert request.url.params["type"] == "2"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": "100",
                            "title": "The Americans",
                            "year": 2013,
                            "thumb": "/library/metadata/100/thumb/1",
                            "Guid": [{"id": "tmdb://46533"}],
                        }
                    ]
                }
            },
        )
    if path == "/library/metadata/100/allLeaves":

        def ep(rk, s, e, file):
            return {
                "ratingKey": rk,
                "parentIndex": s,
                "index": e,
                "title": f"Ep {s}x{e}",
                "summary": f"Synopsis {s}x{e}",
                "duration": 2700000,
                "Media": [{"Part": [{"file": file}]}],
            }

        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        ep("101", 1, 1, "/tv/Americans/S01E01.mkv"),
                        ep("102", 1, 2, "/tv/Americans/S01E02.mkv"),
                        ep("103", 1, 3, "/tv/Americans/S01E03-E04.mkv"),
                        ep("104", 1, 4, "/tv/Americans/S01E03-E04.mkv"),
                        ep("105", 2, 1, "/tv/Americans/S02E01.mkv"),
                        ep("190", 0, 1, "/tv/Americans/S00E01.mkv"),
                        {"ratingKey": "199", "title": "Unmatched"},
                    ]
                }
            },
        )
    return httpx.Response(404)


def history_rows():
    def row(ref, user, rk, s, e, pct, days):
        return {
            "reference_id": ref,
            "user_id": user,
            "friendly_name": f"Viewer {user}",
            "user": f"viewer{user}",
            "rating_key": rk,
            "grandparent_rating_key": "100",
            "grandparent_title": "The Americans",
            "parent_media_index": s,
            "media_index": e,
            "percent_complete": pct,
            "stopped": ts(days),
            "date": ts(days),
            "media_type": "episode",
            "title": f"Ep {s}x{e}",
        }

    return [
        row(1, 11, "101", 1, 1, 98, 40),
        row(2, 11, "102", 1, 2, 95, 30),
        row(3, 11, "103", 1, 3, 50, 25),  # abandoned halfway: not watched
        row(4, 11, "190", 0, 1, 100, 20),  # special: never a boundary
        row(5, 11, "101", 1, 1, 100, 21),  # rewatch of an early episode
        row(6, 22, "103", 1, 3, 92, 2),  # part of a double episode
        row(7, 22, "", "", "", 100, 1),  # malformed: skipped
    ]


def tautulli_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    assert params["apikey"] == "key"
    if params["cmd"] == "get_history":
        rows = history_rows()
        start, length = int(params["start"]), int(params["length"])
        page = rows[start : start + length]
        return httpx.Response(
            200,
            json={
                "response": {
                    "result": "success",
                    "data": {"recordsFiltered": len(rows), "data": page},
                }
            },
        )
    return httpx.Response(200, json={"response": {"result": "error", "message": "bad cmd"}})


@pytest.fixture
def mocked(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "tok")
    monkeypatch.setenv("TAUTULLI_URL", "http://tautulli:8181")
    monkeypatch.setenv("TAUTULLI_API_KEY", "key")
    monkeypatch.setattr(
        sync,
        "plex_client",
        lambda s: PlexClient(s.plex_url, s.plex_token, transport=httpx.MockTransport(plex_handler)),
    )

    class SmallPages(TautulliClient):
        def iter_history(self, **kw):
            return super().iter_history(**{**kw, "page_size": 3})

    monkeypatch.setattr(
        sync,
        "tautulli_client",
        lambda s: SmallPages(
            s.tautulli_url, s.tautulli_api_key, transport=httpx.MockTransport(tautulli_handler)
        ),
    )


def test_library_sync_and_history(mocked):
    with session_scope() as session:
        settings = load_settings(session)
        assert sync.sync_library(session, settings) == {"shows": 1, "episodes": 6}
        show = session.scalar(select(Show))
        assert (show.title, show.tmdb_id, show.year) == ("The Americans", 46533, 2013)
        e3, e4 = (
            session.scalar(select(Episode).where(Episode.episode == n, Episode.season == 1))
            for n in (3, 4)
        )
        assert e4.part_of_episode_id == e3.id and e3.part_of_episode_id is None

        result = sync.sync_history(session, settings)
        assert result["rows"] == 6
        users = {u.plex_user_id: u for u in session.scalars(select(User))}
        assert users["11"].display_name == "Viewer 11"

        b11 = boundary_episode(session, users["11"].id, show.id)
        assert b11.code == "S01E02"  # 50% on E03 doesn't count; rewatch & special don't move it
        progress = session.scalar(
            select(WatchProgress).where(WatchProgress.user_id == users["11"].id)
        )
        assert (now() - progress.last_watched_at).days == 20  # the special still counts as activity

        b22 = boundary_episode(session, users["22"].id, show.id)
        assert b22.code == "S01E04"  # watched E03 of the E03-E04 file

        listing = shows_in_progress(session, users["22"], now())
        assert listing[0]["next_episode"] == "S02E01"
        assert listing[0]["days_since"] == 2

        # A second sync is idempotent.
        sync.sync_history(session, settings)
        assert session.query(WatchEvent).count() == 6


def test_watched_threshold_is_configurable(mocked, monkeypatch):
    monkeypatch.setenv("WATCHED_PERCENT", "40")
    with session_scope() as session:
        settings = load_settings(session)
        sync.sync_library(session, settings)
        sync.sync_history(session, settings)
        user = session.scalar(select(User).where(User.plex_user_id == "11"))
        show = session.scalar(select(Show))
        assert boundary_episode(session, user.id, show.id).code == "S01E04"


def test_history_creates_stub_shows_when_plex_is_missing(mocked, monkeypatch):
    monkeypatch.delenv("PLEX_URL")
    with session_scope() as session:
        sync.sync_history(session, load_settings(session))
        show = session.scalar(select(Show))
        assert show.title == "The Americans" and show.plex_rating_key == "100"
        assert session.query(Episode).count() == 4


def test_tautulli_errors_surface():
    client = TautulliClient("http://t", "key", transport=httpx.MockTransport(tautulli_handler))
    with pytest.raises(Exception, match="bad cmd"):
        client.get_users()
