"""TMDB overviews and per-episode cast: grounding for the model, or summaries without one."""

import httpx
from sqlalchemy import select
from test_pipeline import make_show

from wherewerewe.db import load_settings, session_scope
from wherewerewe.llm.ledger import ledger_as_of
from wherewerewe.models import EpisodeSummary
from wherewerewe.services import indexing
from wherewerewe.services.indexing import index_episode
from wherewerewe.services.library import regular_episodes
from wherewerewe.services.sync import import_folder
from wherewerewe.sources.tmdb import TMDBClient, clean_character, credited_in

OVERVIEWS = {
    (1, 1): "Walt, a chemistry teacher, gets a diagnosis. Jesse makes an offer.",
    (1, 2): "Walt and Jesse must deal with a body.",
    (1, 3): "Walt makes a hard choice about Krazy-8.",
    (2, 1): "A new season begins for Walt.",
    (2, 2): "Hank closes in.",
}
REGULARS = [
    {"character": "Walter White", "actor": "Actor One", "guest": False},
    {"character": "Jesse Pinkman", "actor": "Actor Two", "guest": False},
    {"character": "Hank Schrader", "actor": "Actor Three", "guest": False},
]


FETCHED: list[tuple[int, int]] = []


class FakeTMDB:
    def __init__(self, api_key):
        pass

    def find_show(self, title, year=None):
        return 1396

    def episode(self, tv_id, season, episode):
        FETCHED.append((season, episode))
        guests = [{"character": "Krazy-8", "actor": "Guest Actor", "guest": True}]
        return {
            "overview": OVERVIEWS.get((season, episode)),
            "cast": REGULARS + (guests if (season, episode) == (1, 3) else []),
        }

    def close(self):
        pass


def test_clean_character():
    assert clean_character("Walter White (voice)") == "Walter White"
    assert clean_character("Himself") == ""
    assert clean_character("  Skyler   White ") == "Skyler White"


def test_regulars_count_only_once_the_episode_mentions_them():
    cast = [*REGULARS, {"character": "Tuco", "actor": "X", "guest": True}]
    names = [c["character"] for c in credited_in(cast, "Walt meets Jesse at the lab.")]
    # "Walt" counts for Walter. Hank is credited on every episode but never mentioned here.
    assert names == ["Walter White", "Jesse Pinkman", "Tuco"]


def test_client_parses_overview_and_credits():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["append_to_response"] == "credits"
        return httpx.Response(
            200,
            json={
                "overview": " Things happen. ",
                "credits": {
                    "cast": [{"character": "Walter White", "name": "A"}],
                    "guest_stars": [
                        {"character": "Herself", "name": "B"},
                        {"character": "Tuco Salamanca", "name": "C"},
                        {"character": "walter white", "name": "A"},
                    ],
                },
            },
        )

    client = TMDBClient("k" * 32, transport=httpx.MockTransport(handler))
    data = client.episode(1, 1, 1)
    assert data == {
        "overview": "Things happen.",
        "cast": [
            {"character": "Walter White", "actor": "A", "guest": False},
            {"character": "Tuco Salamanca", "actor": "C", "guest": True},
        ],
    }


def test_synopsis_mode_needs_no_model(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_SOURCE", "synopsis")
    monkeypatch.setenv("TMDB_API_KEY", "k" * 32)
    monkeypatch.setattr(indexing, "TMDBClient", FakeTMDB)
    monkeypatch.setattr(
        indexing, "get_llm", lambda s: (_ for _ in ()).throw(AssertionError("no model"))
    )
    FETCHED.clear()
    with session_scope() as session:
        show_id = import_folder(session, make_show(tmp_path), "The Show")["show_id"]
    with session_scope() as session:
        settings = load_settings(session)
        for ep in regular_episodes(session, show_id):
            index_episode(session, settings, ep.id)
    with session_scope() as session:
        rows = list(session.scalars(select(EpisodeSummary)))
        assert {r.model for r in rows} == {"synopsis"} and len(rows) == 5
        first = next(r for r in rows if r.summary_json["episode"] == "S01E01")
        assert first.summary_json["plot_beats"] == [
            "Walt, a chemistry teacher, gets a diagnosis.",
            "Jesse makes an offer.",
        ]
        people = {e.name: e for e in ledger_as_of(session, show_id, (1, 3))}
        # Hank is credited all season but not mentioned until S02E02.
        assert set(people) == {"Walter White", "Jesse Pinkman", "Krazy-8"}
        # Notes come from what the synopsis says about them; the actor is shown separately.
        assert people["Krazy-8"].role is None
        assert people["Krazy-8"].history[0]["new_info"] == "Walt makes a hard choice about Krazy-8."
        assert "Hank Schrader" in {e.name for e in ledger_as_of(session, show_id, (2, 2))}
    # Each episode is fetched from TMDB once, even when re-indexed.
    with session_scope() as session:
        settings = load_settings(session)
        for ep in regular_episodes(session, show_id):
            index_episode(session, settings, ep.id, force=True)
    assert len(FETCHED) == 5


def test_dialogue_mode_gives_the_model_credited_names(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setenv("TMDB_API_KEY", "k" * 32)
    monkeypatch.setattr(indexing, "TMDBClient", FakeTMDB)
    with session_scope() as session:
        show_id = import_folder(session, make_show(tmp_path), "The Show")["show_id"]
        ep = regular_episodes(session, show_id)[0]
        index_episode(session, load_settings(session), ep.id)
    prompt = next(c for c in fake_llm.calls if c["kind"] == "summary")["messages"][1]["content"]
    assert "Characters credited in this episode" in prompt
    assert "Walter White" in prompt and "Jesse Pinkman" in prompt
    assert "Hank Schrader" not in prompt  # credited, but not in this episode's text


def test_adding_a_tmdb_key_makes_older_summaries_outdated(tmp_path, monkeypatch, fake_llm):
    monkeypatch.setattr(indexing, "TMDBClient", FakeTMDB)
    with session_scope() as session:
        show_id = import_folder(session, make_show(tmp_path), "The Show")["show_id"]
        settings = load_settings(session)
        for ep in regular_episodes(session, show_id):
            index_episode(session, settings, ep.id)
        assert indexing.show_stats(session, show_id, settings)["outdated"] == 0
        assert indexing.queue_show_index(session, show_id, scope="all", settings=settings) == 0

    monkeypatch.setenv("TMDB_API_KEY", "k" * 32)
    with session_scope() as session:
        settings = load_settings(session)
        assert indexing.show_stats(session, show_id, settings)["outdated"] == 5
        assert indexing.queue_show_index(session, show_id, scope="all", settings=settings) == 5
        first = regular_episodes(session, show_id)[0]
        assert "summarized from" in index_episode(session, settings, first.id)
        assert index_episode(session, settings, first.id).endswith("already summarized")
        assert indexing.show_stats(session, show_id, settings)["outdated"] == 4


def test_shows_not_on_tmdb_are_not_outdated_forever(tmp_path, monkeypatch, fake_llm):
    class NotFound(FakeTMDB):
        def find_show(self, title, year=None):
            return None

    monkeypatch.setenv("TMDB_API_KEY", "k" * 32)
    monkeypatch.setattr(indexing, "TMDBClient", NotFound)
    with session_scope() as session:
        show_id = import_folder(session, make_show(tmp_path), "The Show")["show_id"]
        settings = load_settings(session)
        for ep in regular_episodes(session, show_id):
            index_episode(session, settings, ep.id)
        assert indexing.show_stats(session, show_id, settings)["outdated"] == 0


def test_characters_show_who_plays_them_up_to_the_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_SOURCE", "synopsis")
    monkeypatch.setenv("TMDB_API_KEY", "k" * 32)
    monkeypatch.setattr(indexing, "TMDBClient", FakeTMDB)
    with session_scope() as session:
        show_id = import_folder(session, make_show(tmp_path), "The Show")["show_id"]
        settings = load_settings(session)
        for ep in regular_episodes(session, show_id):
            index_episode(session, settings, ep.id)
        actors = indexing.actors_as_of(session, show_id, (1, 2))
        assert "krazy-8" not in actors  # first credited in S01E03
        people = {e.name: e for e in ledger_as_of(session, show_id, (1, 3))}
        actors = indexing.actors_as_of(session, show_id, (1, 3))
        assert indexing.played_by(people["Walter White"], actors) == "Actor One"
        assert indexing.played_by(people["Krazy-8"], actors) == "Guest Actor"


def test_synopsis_summaries_are_refreshed_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMARY_SOURCE", "synopsis")
    monkeypatch.setenv("TMDB_API_KEY", "k" * 32)
    monkeypatch.setattr(indexing, "TMDBClient", FakeTMDB)
    with session_scope() as session:
        show_id = import_folder(session, make_show(tmp_path), "The Show")["show_id"]
        settings = load_settings(session)
        for ep in regular_episodes(session, show_id):
            index_episode(session, settings, ep.id)
        for row in session.scalars(select(EpisodeSummary)):
            row.summary_json = {**row.summary_json, "characters": []}  # an older format
    FETCHED.clear()
    with session_scope() as session:
        assert indexing.refresh_synopsis_summaries(session) == 5
        indexing.rebuild_ledgers(session, load_settings(session))
        assert "Walter White" in {e.name for e in ledger_as_of(session, show_id, (1, 1))}
    assert FETCHED == []  # nothing fetched again
