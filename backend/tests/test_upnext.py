"""Up Next: how history becomes taste, and taste becomes picks with reasons."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from wherewerewe.api.app import create_app
from wherewerewe.db import load_settings, session_scope
from wherewerewe.integrations.sonarr import SonarrClient
from wherewerewe.models import (
    Episode,
    Show,
    ShowMeta,
    TmdbTitle,
    User,
    WatchEvent,
)
from wherewerewe.services import meta, upnext
from wherewerewe.services.progress import recompute_progress
from wherewerewe.sources.tmdb import parse_show


def ago(days: float) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)


CRIME = {"genres": ["Crime", "Drama"], "keywords": ["heist", "detective"], "language": "en"}
COMEDY = {"genres": ["Comedy"], "keywords": ["sitcom", "workplace"], "language": "en"}
REALITY = {"genres": ["Reality"], "keywords": ["dating"], "language": "en"}


def add_show(session, title, tmdb_id, eps=8, seasons=1, related=(), status="Ended", **fields):
    show = Show(title=title, tmdb_id=tmdb_id, plex_rating_key=f"rk{tmdb_id}", year=2020)
    session.add(show)
    session.flush()
    for s in range(1, seasons + 1):
        for e in range(1, eps + 1):
            session.add(
                Episode(
                    show_id=show.id,
                    season=s,
                    episode=e,
                    duration_ms=45 * 60_000,
                    plex_rating_key=f"rk{tmdb_id}-{s}-{e}",
                    air_date="2020-01-01",
                )
            )
    session.add(
        ShowMeta(
            show_id=show.id,
            summary=f"The premise of {title}.",
            runtime_minutes=45,
            tmdb_status=status,
            vote_average=7.5,
            vote_count=500,
            related=[{"tmdb_id": t, "rank": i, "kind": "rec"} for i, t in enumerate(related)],
            plex_fetched_at=ago(1),
            tmdb_fetched_at=ago(1),
            **fields,
        )
    )
    session.flush()
    return show


def watch(session, user, show, upto, days_ago, settings, spread=1.0):
    eps = (
        session.query(Episode)
        .filter(Episode.show_id == show.id)
        .order_by(Episode.season, Episode.episode)
        .all()
    )
    for i, ep in enumerate(eps[:upto]):
        session.add(
            WatchEvent(
                user_id=user.id,
                episode_id=ep.id,
                percent_complete=100,
                watched_at=ago(days_ago + (upto - i) * spread),
            )
        )
    session.flush()
    recompute_progress(session, user.id, show.id, settings.watched_percent)


@pytest.fixture
def house():
    """Alex finished a heist show, binged a sitcom, dropped a dating show after one episode,
    and is midway through a detective show. Sam finished a comedy Alex hasn't seen."""
    with session_scope() as s:
        settings = load_settings(s)
        alex = User(plex_user_id="1", display_name="Alex")
        sam = User(plex_user_id="2", display_name="Sam")
        s.add_all([alex, sam])
        s.flush()
        heist = add_show(s, "The Heist", 101, related=[201, 900], creators=["Jo Maker"], **CRIME)
        sitcom = add_show(s, "Office Life", 102, related=[202], **COMEDY)
        dating = add_show(s, "Love Island Life", 103, **REALITY)
        detective = add_show(s, "Detective Days", 104, **CRIME)
        caper = add_show(s, "The Caper", 201, creators=["Jo Maker"], **CRIME)
        quips = add_show(s, "Quips", 202, **COMEDY)
        roses = add_show(s, "Roses", 203, **REALITY)
        long_crime = add_show(
            s, "Endless Crime", 204, eps=20, seasons=5, status="Returning Series", **CRIME
        )
        s.add(
            TmdbTitle(
                tmdb_id=900,
                name="Elsewhere Heist",
                vote_average=8.1,
                vote_count=900,
                genres=["Crime"],
                language="en",
            )
        )
        watch(s, alex, heist, 8, 40, settings, spread=0.5)
        watch(s, alex, sitcom, 8, 10, settings, spread=0.3)
        watch(s, alex, dating, 1, 90, settings)
        watch(s, alex, detective, 3, 2, settings)
        watch(s, sam, quips, 8, 5, settings, spread=0.2)
        ids = {
            "alex": alex.id,
            "sam": sam.id,
            **{k: v.id for k, v in locals().items() if isinstance(v, Show)},
        }
    return ids


def run(user_id, filters=None, with_user=None, names=True):
    with session_scope() as s:
        ctx = upnext.make_context(s, load_settings(s))
        ctx.names = names
        return upnext.up_next(ctx, user_id, filters or upnext.Filters(), with_user)


def lane(data, key):
    return next((lane for lane in data["lanes"] if lane["key"] == key), {"items": []})["items"]


def titles(items):
    return [i["title"] for i in items]


def test_history_becomes_states_and_affinity(house):
    with session_scope() as s:
        ctx = upnext.make_context(s, load_settings(s))
        w = ctx.profile(house["alex"]).watches
        assert w[house["heist"]].state == "finished" and w[house["heist"]].affinity >= 0.8
        assert w[house["dating"]].state == "sampled" and w[house["dating"]].affinity < 0
        assert w[house["detective"]].state == "watching"


def test_picks_follow_taste_and_related_shows_with_reasons(house):
    data = run(house["alex"])
    start = lane(data, "start")
    names = titles(start)
    assert names.index("The Caper") < names.index("Roses")
    caper = start[names.index("The Caper")]
    texts = [r["text"] for r in caper["reasons"]]
    assert "Because you finished The Heist" in texts
    assert "From Jo Maker, who made The Heist" in texts
    assert caper["parts"]["related"] > 0 and caper["parts"]["taste"] > 0
    # Sam's binge counts for Alex too
    quips = start[names.index("Quips")]
    assert any("Sam" in r["text"] for r in quips["reasons"])
    # Something dropped after one episode pulls its kind of show down
    assert start[names.index("Roses")]["parts"]["taste"] < 0


def test_in_progress_shows_come_first_and_carry_their_next_episode(house):
    data = run(house["alex"])
    keep = lane(data, "keep")
    assert titles(keep) == ["Detective Days"]
    p = keep[0]["progress"]
    assert p["next_code"] == "S01E04" and p["watched"] == 3 and p["left_in_season"] == 5
    assert "rk104-1-4" in (keep[0]["play_url"] or "") or keep[0]["play_url"] is None
    assert data["tonight"]["pick"]["title"] == "Detective Days"


def test_new_episodes_after_you_caught_up(house):
    with session_scope() as s:
        show = s.get(Show, house["sitcom"])
        for e in (1, 2):
            s.add(
                Episode(
                    show_id=show.id,
                    season=2,
                    episode=e,
                    air_date=ago(1).date().isoformat(),
                    duration_ms=1,
                )
            )
    new = lane(run(house["alex"]), "new")
    assert titles(new) == ["Office Life"]
    assert new[0]["progress"]["new_season"] and new[0]["reasons"][0]["text"].startswith(
        "Season 2 is here"
    )


def test_marks_change_the_picks(house):
    client = TestClient(create_app())
    alex = house["alex"]
    key = f"show:{house['caper']}"
    assert client.put(f"/api/users/{alex}/preferences", json={"key": key, "kind": "nope"}).json()[
        "prefs"
    ] == ["nope"]
    assert "The Caper" not in titles(lane(run(alex), "start"))
    # Love replaces Not for me; Save for later puts it on the list
    client.put(f"/api/users/{alex}/preferences", json={"key": key, "kind": "love"})
    res = client.put(f"/api/users/{alex}/preferences", json={"key": key, "kind": "later"})
    assert res.json()["prefs"] == ["later", "love"]
    assert titles(lane(run(alex), "list")) == ["The Caper"]
    off = client.put(
        f"/api/users/{alex}/preferences", json={"key": key, "kind": "later", "on": False}
    )
    assert off.json()["prefs"] == ["love"]


def test_filters_shape_new_show_rows(house):
    complete = titles(lane(run(house["alex"], upnext.Filters(story="complete")), "start"))
    assert "Endless Crime" not in complete and "The Caper" in complete
    long = titles(lane(run(house["alex"], upnext.Filters(time="long")), "start"))
    assert long == ["Endless Crime"]
    comedy = titles(lane(run(house["alex"], upnext.Filters(genre="comedy")), "start"))
    assert comedy == ["Quips"]


def test_not_in_your_library(house):
    beyond = lane(run(house["alex"]), "beyond")
    assert titles(beyond) == ["Elsewhere Heist"]
    assert beyond[0]["reasons"][0]["text"] == "Because you finished The Heist"
    assert beyond[0]["poster"] is None and not beyond[0]["in_library"]


def test_watch_together(house):
    with session_scope() as s:
        settings = load_settings(s)
        sam = s.get(User, house["sam"])
        watch(s, sam, s.get(Show, house["detective"]), 1, 3, settings)
    together = lane(run(house["alex"], with_user=house["sam"]), "together")
    first = together[0]
    assert first["title"] == "Detective Days"
    assert first["reasons"][0]["text"].startswith("You're 2 episodes ahead of Sam")


def test_viewers_see_household_reasons_without_names(house):
    quips = next(i for i in lane(run(house["alex"], names=False), "start") if i["title"] == "Quips")
    assert any(r["text"].startswith("Someone here") for r in quips["reasons"])
    assert not any("Sam" in r["text"] for r in quips["reasons"])


def test_api_and_pitch(house, fake_llm):
    client = TestClient(create_app())
    alex = house["alex"]
    data = client.get(f"/api/users/{alex}/upnext?time=any&genre=crime").json()
    assert data["filters"]["genre"] == "crime" and data["can_add"] is False
    assert client.get(f"/api/users/{alex}/upnext?time=forever").status_code == 422
    item = client.get(f"/api/users/{alex}/upnext/item?key=show:{house['caper']}").json()
    assert item["title"] == "The Caper" and item["reasons"]
    first = client.post(f"/api/users/{alex}/pitch", json={"key": f"show:{house['caper']}"}).json()
    again = client.post(f"/api/users/{alex}/pitch", json={"key": f"show:{house['caper']}"}).json()
    assert first == again
    pitches = [c for c in fake_llm.calls if "help one person decide" in c["messages"][0]["content"]]
    assert len(pitches) == 1  # written once, then kept
    prompt = pitches[0]["messages"][1]["content"]
    assert "The premise of The Caper." in prompt and "The Heist" in prompt
    assert (
        client.put(
            f"/api/users/{alex}/preferences", json={"key": "bad", "kind": "love"}
        ).status_code
        == 400
    )


def test_viewers_only_get_their_own(house, monkeypatch):
    from test_auth import FakePlex, FakePlexTV, sign_in_with_plex

    from wherewerewe.api import auth_routes

    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setattr(auth_routes.plextv, "PlexTV", FakePlexTV)
    monkeypatch.setattr(auth_routes, "plex_client", lambda settings: FakePlex())
    FakePlexTV.account_id = "1"  # Alex
    client = TestClient(create_app(), follow_redirects=False)
    sign_in_with_plex(client)
    alex, sam = house["alex"], house["sam"]
    assert client.get("/upnext").status_code == 200
    ok = client.get(f"/api/users/{alex}/upnext?with_user={sam}").json()
    assert not any(lane["key"] == "together" for lane in ok["lanes"])  # admins only
    assert client.get(f"/api/users/{sam}/upnext").status_code == 403
    assert (
        client.put(
            f"/api/users/{sam}/preferences", json={"key": "show:1", "kind": "love"}
        ).status_code
        == 403
    )
    assert client.post("/api/upnext/refresh").status_code == 403
    assert client.post("/api/sonarr/add", json={"tmdb_id": 900}).status_code == 403


def test_parse_tmdb_show():
    data = parse_show(
        {
            "name": "X",
            "overview": " Premise. ",
            "status": "Ended",
            "original_language": "en",
            "genres": [{"name": "Crime"}],
            "keywords": {"results": [{"name": "heist"}]},
            "created_by": [{"name": "Jo Maker"}],
            "networks": [{"name": "HBO"}],
            "number_of_seasons": 2,
            "number_of_episodes": 16,
            "episode_run_time": [52, 48],
            "vote_average": 8.2,
            "vote_count": 1200,
            "external_ids": {"tvdb_id": 4242},
            "recommendations": {
                "results": [
                    {
                        "id": 7,
                        "name": "Y",
                        "genre_ids": [80],
                        "first_air_date": "2019-03-01",
                        "original_language": "ko",
                    }
                ]
            },
            "similar": {"results": [{"id": 7, "name": "Y"}, {"id": 8, "name": "Z", "adult": True}]},
        }
    )
    assert data["overview"] == "Premise." and data["runtime"] == 48 and data["tvdb_id"] == 4242
    assert data["related"] == [
        {"tmdb_id": 7, "rank": 0, "kind": "rec"},
        {"tmdb_id": 7, "rank": 0, "kind": "similar"},
    ]
    assert data["titles"] == [
        {
            "tmdb_id": 7,
            "name": "Y",
            "year": 2019,
            "overview": None,
            "poster_path": None,
            "backdrop_path": None,
            "genres": ["Crime"],
            "language": "ko",
            "vote_average": None,
            "vote_count": None,
        }
    ]


def test_tmdb_details_are_fetched_in_batches(house, monkeypatch):
    fetched = []

    class FakeTMDB:
        def __init__(self, key):
            pass

        def show(self, tv_id):
            fetched.append(tv_id)
            return parse_show(
                {
                    "name": "x",
                    "status": "Ended",
                    "recommendations": {"results": [{"id": 5000 + tv_id, "name": "New"}]},
                }
            )

        def find_show(self, title, year=None):
            return None

        def close(self):
            pass

    monkeypatch.setattr(meta, "TMDBClient", FakeTMDB)
    monkeypatch.setenv("TMDB_API_KEY", "k" * 32)
    with session_scope() as s:
        for m in s.query(ShowMeta):
            m.tmdb_fetched_at = None
    with session_scope() as s:
        settings = load_settings(s)
        assert meta.sync_tmdb_meta(s, settings, limit=5, pause=0) == (5, 3)
        assert meta.sync_tmdb_meta(s, settings, limit=5, pause=0) == (3, 0)
        assert meta.sync_tmdb_meta(s, settings, limit=5, pause=0) == (0, 0)
        assert s.get(TmdbTitle, 5101).name == "New"
    # Shows someone watched go first
    assert set(fetched[:5]) >= {101, 102, 103, 104}


def test_sonarr_add():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        assert request.headers["X-Api-Key"] == "key"
        path = request.url.path
        if path.endswith("/series/lookup"):
            assert request.url.params["term"] == "tvdb:4242"
            return httpx.Response(200, json=[{"title": "Y", "tvdbId": 4242, "seasons": []}])
        if path.endswith("/qualityprofile"):
            return httpx.Response(
                200, json=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
            )
        if path.endswith("/rootfolder"):
            return httpx.Response(200, json=[{"id": 1, "path": "/tv"}])
        if path.endswith("/languageprofile"):
            return httpx.Response(404)
        if path.endswith("/series") and request.method == "POST":
            return httpx.Response(201, json={"title": "Y", "id": 9})
        return httpx.Response(500)

    client = SonarrClient("http://sonarr:8989", "key", transport=httpx.MockTransport(handler))
    assert client.add(4242, quality_profile="HD-1080p") == {"title": "Y", "added": True}
    import json

    body = json.loads(calls[-1][2])
    assert body["qualityProfileId"] == 4 and body["rootFolderPath"] == "/tv"
    assert body["addOptions"] == {"monitor": "all", "searchForMissingEpisodes": True}
    assert "languageProfileId" not in body
