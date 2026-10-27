"""Folder import -> indexing -> ledger -> rollups -> recap assembly, with a fake model."""

from pathlib import Path

import pytest
from conftest import write_srt
from sqlalchemy import select

from wherewerewe.db import load_settings, session_scope
from wherewerewe.llm.ledger import ledger_as_of, match_character, rebuild_ledger
from wherewerewe.llm.recap import build_context
from wherewerewe.models import (
    Character,
    Episode,
    EpisodeSummary,
    Job,
    Recap,
    SeasonRollup,
    Show,
)
from wherewerewe.services import recaps
from wherewerewe.services.indexing import index_episode
from wherewerewe.services.library import episode_by_code, regular_episodes
from wherewerewe.services.sync import import_folder
from wherewerewe.worker.runner import run_pending

DIALOGUE = {
    (1, 1): ["Walt: I am a teacher.", "Jesse: Yo.", "Walt: We cook."],
    (1, 2): ["Jesse: The tub!", "Walt: Acid."],
    (1, 3): ["Walt: Krazy-8 has to go."],
    (2, 1): ["Walt: Season two now.", "Hank: DEA business."],
    (2, 2): ["Hank: I'm onto something.", "Gus: Welcome to my restaurant."],
}


def make_show(tmp_path: Path, extra: dict | None = None) -> Path:
    folder = tmp_path / "Show"
    folder.mkdir()
    for (s, e), lines in {**DIALOGUE, **(extra or {})}.items():
        write_srt(folder / f"The.Show.S{s:02d}E{e:02d}.en.srt", lines)
    write_srt(folder / "The.Show.S00E01.en.srt", ["Walt: A special."])
    return folder


def index_all(show_id: int) -> None:
    with session_scope() as session:
        settings = load_settings(session)
        for ep in regular_episodes(session, show_id):
            index_episode(session, settings, ep.id)


def test_index_and_recap_never_include_later_episodes(tmp_path, fake_llm):
    folder = make_show(tmp_path)
    with session_scope() as session:
        show_id = import_folder(session, folder, "The Show")["show_id"]
    index_all(show_id)
    run_pending()  # rollup jobs queued when each season completed

    with session_scope() as session:
        assert session.scalar(select(EpisodeSummary).limit(1)).source_type == "sidecar"
        rollups = {r.season for r in session.scalars(select(SeasonRollup))}
        assert rollups == {1, 2}
        settings = load_settings(session)
        show = session.get(Episode, episode_by_code(session, show_id, "S02E01").id).show
        boundary = episode_by_code(session, show_id, "S02E01")
        ctx = build_context(session, show, boundary, "standard", settings, 16384)

    assert "Season 1 rollup" in ctx.text  # earlier season comes from its rollup
    assert "S02E01" in ctx.text
    assert "S02E02" not in ctx.text and "Gus" not in ctx.text  # after the boundary
    assert "The Show" not in ctx.text  # title withheld from the model
    assert "thread from S02E01" in ctx.text
    assert ctx.coverage["watched"] == 4 and ctx.coverage["missing"] == []


def test_ledger_is_limited_to_boundary(tmp_path, fake_llm):
    folder = make_show(tmp_path)
    with session_scope() as session:
        show_id = import_folder(session, folder, "The Show")["show_id"]
    index_all(show_id)
    with session_scope() as session:
        early = {e.name: e for e in ledger_as_of(session, show_id, (1, 2))}
        assert set(early) == {"Walt", "Jesse"}
        assert early["Walt"].status == "injured"
        assert early["Walt"].last_seen == "S01E02"
        late = {e.name for e in ledger_as_of(session, show_id, (2, 2))}
        assert {"Hank", "Gus"} <= late


def test_character_matching():
    chars = [
        Character(id=1, show_id=1, name="Frank Gaad", aliases=[]),
        Character(id=2, show_id=1, name="Stan Beeman", aliases=["Stan"]),
    ]
    assert match_character(chars, "Agent Gaad", []).id == 1
    assert match_character(chars, "Gaad", []).id == 1
    assert match_character(chars, "Stan", []).id == 2
    assert match_character(chars, "Martha", []) is None
    chars.append(Character(id=3, show_id=1, name="Frank Jones", aliases=[]))
    assert match_character(chars, "Frank", []) is None  # ambiguous


def test_ledger_keeps_family_members_apart():
    """Shared kinship words, titles and surnames must not merge different people."""

    def person(name, aliases=(), status="alive"):
        return {"name": name, "aliases": list(aliases), "role": f"{name} role", "status": status}

    with session_scope() as session:
        show = Show(title="Family")
        session.add(show)
        session.flush()
        eps = [Episode(show_id=show.id, season=1, episode=n) for n in (1, 2, 3)]
        session.add_all(eps)
        session.flush()
        summaries = [
            [
                person("Eli Gemstone", ["Daddy", "Pastor Gemstone", "Eli"]),
                person("Jesse Gemstone", ["Daddy", "Pastor Gemstone", "Eli Gemstone"]),
                person("Judy Gemstone", ["Mama", "Jesse"]),
                person("Aimee-Leigh Gemstone", ["Mama", "Aimee-Leigh"], status="dead"),
                person("Lori Milsap", ["Cobb's wife", "Miss Lori"]),
                person("Dr. Carmichael", status="teacher"),
            ],
            [
                person("Jesse"),
                person("Kelvin", ["K"]),
                person("Dr. Gemstone"),  # ambiguous: which Gemstone?
                person("Jim Halpert", ["Andrew Bernard"]),
                person("Andrew Bernard", ["Andy"]),
            ],
            [person("Kelvin Gemstone"), person("Carmichael", status="single")],
        ]
        rebuild_ledger(
            session, show.id, [(e, {"characters": s}) for e, s in zip(eps, summaries, strict=True)]
        )
        entries = {e.name: e for e in ledger_as_of(session, show.id, (1, 3))}

        assert sorted(entries) == [
            "Aimee-Leigh Gemstone",
            "Andrew Bernard",
            "Carmichael",
            "Eli Gemstone",
            "Jesse",
            "Jim Halpert",
            "Judy Gemstone",
            "Kelvin Gemstone",
            "Lori Milsap",
        ]
        assert entries["Jesse"].aliases == ["Jesse Gemstone"]
        assert entries["Eli Gemstone"].aliases == ["Eli"]
        assert entries["Judy Gemstone"].aliases == []
        assert entries["Lori Milsap"].aliases == ["Miss Lori"]
        assert entries["Jim Halpert"].aliases == []
        assert entries["Kelvin Gemstone"].aliases == ["Kelvin", "K"]
        assert entries["Aimee-Leigh Gemstone"].status == "dead"
        assert entries["Carmichael"].status is None  # "teacher", "single" aren't statuses
        assert entries["Carmichael"].appearances == 2


def test_multi_episode_file_is_summarized_once(tmp_path, fake_llm):
    folder = tmp_path / "Show"
    folder.mkdir()
    write_srt(folder / "Show.S01E01-E02.en.srt", ["Walt: Double episode."])
    write_srt(folder / "Show.S01E03.en.srt", ["Walt: Single."])
    with session_scope() as session:
        show_id = import_folder(session, folder, "Show")["show_id"]
        e1, e2 = (episode_by_code(session, show_id, c) for c in ("S01E01", "S01E02"))
        assert e2.part_of_episode_id == e1.id
    index_all(show_id)
    with session_scope() as session:
        summaries = list(session.scalars(select(EpisodeSummary)))
        assert len(summaries) == 2
        assert summaries[0].summary_json["episode"] == "S01E01-E02"
        settings = load_settings(session)
        boundary = episode_by_code(session, show_id, "S01E01")
        ctx = build_context(session, boundary.show, boundary, "quick", settings, 16384)
        # Watching part of a double episode means the whole file was watched.
        assert ctx.boundary_label == "S01E02"
        assert "S01E03" not in ctx.text


def test_specials_are_not_boundaries(tmp_path, fake_llm):
    folder = make_show(tmp_path)
    with session_scope() as session:
        show_id = import_folder(session, folder, "The Show")["show_id"]
        special = session.scalar(select(Episode).where(Episode.season == 0))
        with pytest.raises(recaps.RecapRequestError):
            recaps.create_recap(
                session,
                load_settings(session),
                show_id=show_id,
                length="quick",
                boundary_episode_id=special.id,
            )


def test_run_recap_streams_verifies_and_reports_coverage(tmp_path, fake_llm):
    folder = make_show(tmp_path)
    with session_scope() as session:
        show_id = import_folder(session, folder, "The Show")["show_id"]
        settings = load_settings(session)
        # Only summarize S01E01; the recap should index the latest missing ones itself.
        index_episode(session, settings, episode_by_code(session, show_id, "S01E01").id)
        boundary = episode_by_code(session, show_id, "S01E03")
        recap = recaps.create_recap(
            session, settings, show_id=show_id, length="standard", boundary_episode_id=boundary.id
        )
        recap_id = recap.id
    fake_llm.recap_text = "Walt cooks with Jesse. Gus watches from afar. Things are tense."
    run_pending()
    with session_scope() as session:
        recap = session.get(Recap, recap_id)
        assert recap.status == "done"
        assert recap.text == "Walt cooks with Jesse. Things are tense."
        assert recap.verification_json["removed"][0]["sentence"] == "Gus watches from afar."
        assert recap.coverage_json["missing"] == []  # S01E02 and S01E03 indexed on demand
        assert recap.draft_text.startswith("Walt cooks")
        assert session.scalar(select(Job).where(Job.type == "generate_recap")).status == "done"


def test_recap_with_nothing_summarized_fails_cleanly(tmp_path, fake_llm, monkeypatch):
    folder = make_show(tmp_path)
    with session_scope() as session:
        show_id = import_folder(session, folder, "The Show")["show_id"]
        boundary = episode_by_code(session, show_id, "S02E02")
        recap = recaps.create_recap(
            session,
            load_settings(session),
            show_id=show_id,
            length="quick",
            boundary_episode_id=boundary.id,
        )
        recap_id = recap.id
    monkeypatch.setattr(
        "wherewerewe.services.recaps.index_episode",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no GPU")),
    )
    for _ in range(3):  # retries with backoff; force them due now
        with session_scope() as session:
            for job in session.scalars(select(Job)):
                job.run_after = job.created_at
        run_pending()
    with session_scope() as session:
        recap = session.get(Recap, recap_id)
        job = session.scalar(select(Job).where(Job.type == "generate_recap"))
        assert job.status == "failed" and job.attempts == 3
        assert recap.status == "failed"
        assert "index the show first" in recap.error


def test_long_transcripts_are_chunked(tmp_path, fake_llm):
    fake_llm.context_tokens = 4000
    folder = tmp_path / "Show"
    folder.mkdir()
    write_srt(
        folder / "Show.S01E01.en.srt",
        [f"Walt: line number {i} with some extra words to pad it out." for i in range(600)],
    )
    with session_scope() as session:
        show_id = import_folder(session, folder, "Show")["show_id"]
    index_all(show_id)
    kinds = [c["kind"] for c in fake_llm.calls]
    assert kinds.count("chunk") >= 2 and kinds[-1] == "summary"
