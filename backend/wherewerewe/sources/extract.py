"""Embedded subtitle and audio extraction with ffmpeg/ffprobe."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


class FFmpegError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@dataclass
class Track:
    position: int  # N in "-map 0:s:N" / "0:a:N" (position among streams of that type)
    codec: str
    language: str | None
    title: str | None
    forced: bool
    hearing_impaired: bool
    default: bool

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_CODECS


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise FFmpegError(f"{cmd[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{cmd[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise FFmpegError(f"{cmd[0]} failed: {proc.stderr.strip()[-500:]}")
    return proc


def probe_tracks(video: Path, kind: str) -> list[Track]:
    """List subtitle ("s") or audio ("a") streams in a file."""
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            kind,
            "-show_entries",
            "stream=index,codec_name:stream_tags=language,title"
            ":stream_disposition=default,forced,hearing_impaired",
            "-of",
            "json",
            str(video),
        ],
        timeout=60,
    )
    streams = json.loads(proc.stdout or "{}").get("streams", [])
    tracks = []
    for position, s in enumerate(streams):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        title = tags.get("title")
        title_l = (title or "").lower()
        tracks.append(
            Track(
                position=position,
                codec=(s.get("codec_name") or "").lower(),
                language=(tags.get("language") or "").lower() or None,
                title=title,
                forced=bool(disp.get("forced")) or "forced" in title_l,
                hearing_impaired=bool(disp.get("hearing_impaired"))
                or "sdh" in title_l
                or "cc" in title_l.split(),
                default=bool(disp.get("default")),
            )
        )
    return tracks


def _lang_rank(language: str | None, languages: list[str]) -> int:
    if language and language in languages:
        return languages.index(language)
    if language in (None, "und"):
        return len(languages)
    return len(languages) + 1


def rank_subtitle_tracks(tracks: list[Track], languages: list[str]) -> list[Track]:
    """Text tracks in a preferred (or unknown) language, best first. Forced tracks are skipped."""
    usable = [
        t
        for t in tracks
        if t.is_text and not t.forced and _lang_rank(t.language, languages) <= len(languages)
    ]
    usable.sort(
        key=lambda t: (
            _lang_rank(t.language, languages),
            not t.hearing_impaired,  # SDH tracks often carry speaker labels
            not t.default,
            t.position,
        )
    )
    return usable


def extract_subtitle_track(video: Path, position: int, out_path: Path) -> Path:
    """Extract one embedded text subtitle track as SRT."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-map",
            f"0:s:{position}",
            "-c:s",
            "srt",
            str(out_path),
        ],
        timeout=600,
    )
    return out_path


def pick_audio_track(video: Path, languages: list[str]) -> int:
    tracks = probe_tracks(video, "a")
    if not tracks:
        raise FFmpegError("no audio track")
    tracks.sort(key=lambda t: (_lang_rank(t.language, languages), not t.default, t.position))
    return tracks[0].position


def extract_audio(video: Path, out_wav: Path, languages: list[str]) -> Path:
    """Extract 16 kHz mono WAV for Whisper."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    position = pick_audio_track(video, languages)
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-map",
            f"0:a:{position}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(out_wav),
        ],
        timeout=1800,
    )
    return out_wav
