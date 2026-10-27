from pathlib import Path

from conftest import write_srt

from wherewerewe.sources.paths import map_path, parse_path_map
from wherewerewe.sources.subtitles import (
    clean_subtitle_lines,
    find_sidecars,
    load_clean_subtitles,
    quality_score,
)


def test_clean_strips_tags_lyrics_credits_and_most_cues():
    events = [
        "<i>Where were you?</i>",
        "{\\an8}I was at work.",
        "♪ la la la ♪",
        "[music playing]",
        "[gunshot]",
        "- Get down!\n- Who's shooting?",
        "Subtitles by ExampleSubs",
        "www.opensubtitles.org",
        "STAN: Let's go.",
        "(DOOR SLAMS)",
        "I was at work.",
        "I was at work.",
    ]
    text = clean_subtitle_lines(events).splitlines()
    assert text == [
        "Where were you?",
        "I was at work.",
        "[gunshot]",
        "Get down!",
        "Who's shooting?",
        "Stan: Let's go.",
        "[door slams]",
        "I was at work.",
    ]


def test_clean_caps_kept_cues_and_drops_repeats():
    events = ["[gunshot]"] * 3 + ["[phone rings]", "[gunshot]"]
    assert clean_subtitle_lines(events, max_cues=2).splitlines() == ["[gunshot]", "[phone rings]"]


def test_load_srt_in_cp1252(tmp_path: Path):
    path = tmp_path / "ep.srt"
    path.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nCafé au lait\n".encode("cp1252"))
    assert load_clean_subtitles(path) == "Café au lait"


def test_quality_score_scales_with_density():
    assert quality_score("word " * 4000, 50 * 60000) == 1.0
    assert quality_score("word " * 100, 45 * 60000) < 0.2
    assert quality_score("", None) == 0.0


def test_find_sidecars_prefers_language_and_skips_forced(tmp_path: Path):
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"")
    for name in [
        "Show.S01E01.fr.srt",
        "Show.S01E01.en.forced.srt",
        "Show.S01E01.srt",
        "Show.S01E01.en.sdh.srt",
        "Show.S01E01.en.srt",
        "Other.S01E02.en.srt",
    ]:
        write_srt(tmp_path / name, ["hello"])
    (tmp_path / "Subs").mkdir()
    write_srt(tmp_path / "Subs" / "Show.S01E01.eng.srt", ["hello"])
    found = [s.path.relative_to(tmp_path).as_posix() for s in find_sidecars(video, ["en", "eng"])]
    assert found == [
        "Show.S01E01.en.sdh.srt",
        "Show.S01E01.en.srt",
        "Subs/Show.S01E01.eng.srt",
        "Show.S01E01.srt",
    ]


def test_path_map(tmp_path: Path):
    local = tmp_path / "media" / "tv" / "Show"
    local.mkdir(parents=True)
    (local / "ep.mkv").write_bytes(b"")
    raw = f"/data=/nowhere; /data/tv={tmp_path / 'media' / 'tv'}"
    assert parse_path_map(raw)[0][0] == "/data/tv"  # longest prefix first
    assert map_path("/data/tv/Show/ep.mkv", raw) == local / "ep.mkv"
    assert map_path("/data/tvshows/ep.mkv", raw) is None  # prefix must end at a separator
    assert map_path(str(local / "ep.mkv"), "") == local / "ep.mkv"
    assert map_path(None, raw) is None
