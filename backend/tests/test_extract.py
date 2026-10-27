"""Embedded subtitle extraction with real ffmpeg (skipped when ffmpeg isn't installed)."""

import subprocess
from pathlib import Path

import pytest
from conftest import write_srt

from wherewerewe.config import Settings
from wherewerewe.sources import extract
from wherewerewe.sources.resolver import NoSourceError, resolve_source

pytestmark = [
    pytest.mark.ffmpeg,
    pytest.mark.skipif(not extract.ffmpeg_available(), reason="ffmpeg not installed"),
]

DIALOGUE = [f"Walt: This is line {i} of the real English dialogue track." for i in range(60)]


def make_video(tmp_path: Path, tracks: list[tuple[list[str], str, str]]) -> Path:
    """tracks: (lines, language, disposition) -> an MKV with those subtitle streams."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:d=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
    ]
    for i, (lines, _lang, _disp) in enumerate(tracks):
        srt = tmp_path / f"in{i}.srt"
        write_srt(srt, lines)
        cmd += ["-i", str(srt)]
    cmd += ["-map", "0:v", "-map", "1:a"]
    for i in range(len(tracks)):
        cmd += ["-map", f"{i + 2}:s"]
    for i, (_lines, lang, disp) in enumerate(tracks):
        cmd += [f"-metadata:s:s:{i}", f"language={lang}", f"-disposition:s:{i}", disp]
    out = tmp_path / "Show.S01E01.mkv"
    cmd += ["-c:v", "libx264", "-c:a", "aac", "-c:s", "srt", "-shortest", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def test_embedded_english_track_is_chosen_over_forced_and_foreign(tmp_path):
    video = make_video(
        tmp_path,
        [
            (["Bonjour."] * 60, "fre", "default"),
            (["[speaking Russian]", "Only the foreign bits."], "eng", "forced"),
            (DIALOGUE, "eng", "0"),
        ],
    )
    tracks = extract.probe_tracks(video, "s")
    assert [t.language for t in tracks] == ["fre", "eng", "eng"]
    assert tracks[1].forced and not tracks[2].forced
    ranked = extract.rank_subtitle_tracks(tracks, ["en", "eng"])
    assert [t.position for t in ranked] == [2]
    result = resolve_source(str(video), 3 * 60000, Settings(whisper_enabled=False))
    assert result.source_type == "embedded"
    assert result.text.splitlines()[0] == "Walt: This is line 0 of the real English dialogue track."


def test_sidecar_beats_embedded(tmp_path):
    video = make_video(tmp_path, [(DIALOGUE, "eng", "0")])
    write_srt(
        tmp_path / "Show.S01E01.en.srt",
        [f"Sidecar line {i} with plenty of words." for i in range(60)],
    )
    result = resolve_source(str(video), 60000, Settings(whisper_enabled=False))
    assert result.source_type == "sidecar"


def test_audio_extraction(tmp_path):
    video = make_video(tmp_path, [])
    wav = extract.extract_audio(video, tmp_path / "a.wav", ["en"])
    assert wav.stat().st_size > 1000


def test_no_text_source(tmp_path):
    video = make_video(tmp_path, [])
    with pytest.raises(NoSourceError, match="no usable dialogue"):
        resolve_source(str(video), 60000, Settings(whisper_enabled=False))
