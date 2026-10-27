"""Pick the best available dialogue text for an episode.

Order: sidecar subtitle file -> embedded text subtitle track -> Whisper transcription.
Each candidate is cleaned and scored; low-density text (forced-narrative tracks,
broken files) falls through to the next candidate.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from . import extract, whisper
from .paths import map_path
from .subtitles import find_sidecars, load_clean_subtitles, quality_score, subtitle_span_ms

log = logging.getLogger(__name__)

MIN_QUALITY = 0.2


@dataclass
class SourceResult:
    source_type: str  # sidecar, embedded, whisper
    text: str
    quality: float
    detail: str


class NoSourceError(RuntimeError):
    """No dialogue could be found for an episode."""


def resolve_source(
    file_path: str | None,
    duration_ms: int | None,
    settings: Settings,
    attempts: list[str] | None = None,
) -> SourceResult:
    attempts = attempts if attempts is not None else []
    video = map_path(file_path, settings.media_path_map)
    if video is None:
        raise NoSourceError(
            f"media file not found: {file_path!r} (check MEDIA_PATH_MAP and the media mount)"
        )
    languages = settings.languages
    best: SourceResult | None = None

    def consider(result: SourceResult) -> bool:
        nonlocal best
        attempts.append(f"{result.detail}: quality {result.quality}")
        if best is None or result.quality > best.quality:
            best = result
        return result.quality >= MIN_QUALITY

    for sidecar in find_sidecars(video, languages):
        try:
            text = load_clean_subtitles(sidecar.path)
        except Exception as exc:
            attempts.append(f"sidecar {sidecar.path.name}: {exc}")
            continue
        span = duration_ms or subtitle_span_ms(sidecar.path)
        result = SourceResult(
            "sidecar", text, quality_score(text, span), f"sidecar {sidecar.path.name}"
        )
        if consider(result):
            return result

    if extract.ffmpeg_available():
        try:
            tracks = extract.rank_subtitle_tracks(extract.probe_tracks(video, "s"), languages)
        except extract.FFmpegError as exc:
            attempts.append(f"ffprobe: {exc}")
            tracks = []
        for track in tracks:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    srt = extract.extract_subtitle_track(
                        video, track.position, Path(tmp) / "track.srt"
                    )
                    text = load_clean_subtitles(srt)
                    span = duration_ms or subtitle_span_ms(srt)
                except Exception as exc:
                    attempts.append(f"embedded track {track.position}: {exc}")
                    continue
            label = f"embedded track {track.position} ({track.codec}, {track.language or 'und'})"
            result = SourceResult("embedded", text, quality_score(text, span), label)
            if consider(result):
                return result
    else:
        attempts.append("ffmpeg not installed: skipped embedded subtitles and Whisper")

    if settings.whisper_enabled and whisper.whisper_installed() and extract.ffmpeg_available():
        lang = next((x for x in languages if len(x) == 2), None)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                wav = extract.extract_audio(video, Path(tmp) / "audio.wav", languages)
                text = whisper.transcribe(
                    wav,
                    settings.whisper_model,
                    settings.whisper_device,
                    settings.whisper_compute_type,
                    language=lang,
                )
            except Exception as exc:
                attempts.append(f"whisper: {exc}")
                text = ""
        if text:
            # Whisper output is the last resort, so accept it whatever its density.
            result = SourceResult(
                "whisper",
                text,
                quality_score(text, duration_ms),
                f"whisper {settings.whisper_model}",
            )
            attempts.append(f"{result.detail}: quality {result.quality}")
            return result
    elif settings.whisper_enabled and not whisper.whisper_installed():
        attempts.append("whisper: faster-whisper not installed")

    if best is not None and best.text.strip():
        log.warning("using low-quality source for %s: %s", video, best.detail)
        return best
    raise NoSourceError("no usable dialogue source; tried: " + "; ".join(attempts or ["nothing"]))
