"""Subtitle loading, cleaning, and sidecar discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pysubs2

SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt")

# SDH cues worth keeping because they carry plot information. Everything else
# ([music playing], [chuckles], (sighs)...) is dropped.
_PLOT_CUE = re.compile(
    r"gun|shot|scream|explo|crash|siren|crying|sob|knock|phone|ring|door|glass|thud|gasp|"
    r"alarm|bang|fire|punch|slap|kiss|whisper|engine|tires|footsteps|groan|grunt|choke|thunder",
    re.IGNORECASE,
)
_CUE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")
_MUSIC = re.compile(r"[♪♫]|^#|#$")
_CREDIT = re.compile(
    r"https?://|www\.|\b(subtitles?|subs|captions?|captioning)\s+(by|from|provided)|"
    r"\bsync(ed|hed)?\s+(and\s+corrected\s+)?by\b|\bcorrected\s+by\b|opensubtitles|addic7ed|"
    r"\bripped\s+by\b|\bencoded\s+by\b",
    re.IGNORECASE,
)
_SPEAKER = re.compile(r"^([A-Z][A-Z0-9 .'\-]{1,30}):\s*(.*)$")


def _load(path: Path) -> pysubs2.SSAFile:
    if path.stat().st_size == 0:
        return pysubs2.SSAFile()  # ffmpeg writes an empty file for a track with no cues
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return pysubs2.load(str(path), encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
        except pysubs2.exceptions.FormatAutodetectionError:
            return pysubs2.SSAFile()  # not a subtitle format we can read: no dialogue
    raise ValueError(f"could not decode {path}") from last_error  # pragma: no cover


def subtitle_span_ms(path: Path) -> int | None:
    """Time from the first to the last cue; a duration estimate when the video's is unknown."""
    events = [e for e in _load(path) if not e.is_comment]
    if not events:
        return None
    return max(e.end for e in events) - min(e.start for e in events) or None


def read_subtitle_file(path: Path) -> list[str]:
    """Return the plain text of each subtitle event, in order."""
    subs = _load(path)
    lines: list[str] = []
    for event in subs:
        if event.is_comment:
            continue
        text = event.plaintext
        if text.strip():
            lines.append(text)
    return lines


def _clean_line(line: str, state: dict) -> list[str]:
    line = _TAG.sub("", line).replace("​", "").strip()
    if not line or _MUSIC.search(line) or _CREDIT.search(line):
        return []

    out: list[str] = []

    def keep_cue(match: re.Match[str]) -> str:
        cue = match.group(0)[1:-1].strip().lower()
        if (
            cue
            and _PLOT_CUE.search(cue)
            and state["cues"] < state["max_cues"]
            and cue != state["last_cue"]
        ):
            state["cues"] += 1
            state["last_cue"] = cue
            out.append(f"[{cue}]")
        return " "

    line = _CUE.sub(keep_cue, line)
    line = re.sub(r"\s+", " ", line).strip(" -–—")
    if not line or not re.search(r"\w", line):
        return out

    speaker = _SPEAKER.match(line)
    if speaker:
        name = speaker.group(1).strip().title()
        rest = speaker.group(2).strip()
        line = f"{name}: {rest}" if rest else ""
    if line:
        out.append(line)
    return out


def clean_subtitle_lines(events: list[str], max_cues: int = 40) -> str:
    """Turn raw subtitle events into one utterance per line for the LLM.

    Strips formatting tags, song lyrics, credits and most SDH cues (a few
    plot-relevant ones are kept), splits "- line one / - line two" dialogue,
    keeps speaker labels like "STAN: ..." because they help attribution, and
    collapses repeated lines (common in broadcast roll-up captions).
    """
    state = {"cues": 0, "last_cue": None, "max_cues": max_cues}
    result: list[str] = []
    previous = ""
    for event in events:
        for part in re.split(r"\n|\\N", event):
            for line in _clean_line(part, state):
                normalized = re.sub(r"\W+", " ", line).strip().lower()
                if not normalized or normalized == previous:
                    continue
                previous = normalized
                result.append(line)
    return "\n".join(result)


def load_clean_subtitles(path: Path) -> str:
    return clean_subtitle_lines(read_subtitle_file(path))


def quality_score(text: str, duration_ms: int | None) -> float:
    """Rough dialogue density: 1.0 at 80+ words per minute.

    Forced-narrative tracks (foreign dialogue only) and broken files score very low,
    which lets the resolver move on to the next source.
    """
    words = len(text.split())
    minutes = (duration_ms or 0) / 60000 or 42.0
    return round(min(1.0, words / minutes / 80.0), 3)


@dataclass
class Sidecar:
    path: Path
    language: str | None
    forced: bool
    sdh: bool


_LANG_TAG = re.compile(r"^[a-z]{2,3}([-_][a-z]{2,4})?$", re.IGNORECASE)
_NOT_LANG = {"sdh", "cc", "hi", "forced", "default", "full"}


def _parse_sidecar(path: Path, video_stem: str) -> Sidecar | None:
    name = path.name
    if not name.lower().endswith(SUBTITLE_EXTENSIONS):
        return None
    stem = name.rsplit(".", 1)[0]
    if stem == video_stem:
        tags: list[str] = []
    elif stem.startswith(video_stem + "."):
        tags = [t.lower() for t in stem[len(video_stem) + 1 :].split(".") if t]
    else:
        return None
    language = next((t for t in tags if t not in _NOT_LANG and _LANG_TAG.match(t)), None)
    return Sidecar(
        path=path,
        language=language,
        forced="forced" in tags,
        sdh=bool({"sdh", "cc", "hi"} & set(tags)),
    )


def find_sidecars(video: Path, languages: list[str]) -> list[Sidecar]:
    """Subtitle files next to the video (or in Subs/), best first. Forced tracks are excluded."""
    found: list[Sidecar] = []
    for folder in (video.parent, video.parent / "Subs", video.parent / "subs"):
        if not folder.is_dir():
            continue
        for candidate in sorted(folder.iterdir()):
            sidecar = _parse_sidecar(candidate, video.stem)
            if sidecar and not sidecar.forced:
                found.append(sidecar)

    def rank(s: Sidecar) -> tuple:
        lang = (s.language or "").split("-")[0].split("_")[0]
        if lang in languages:
            lang_rank = languages.index(lang)
        elif s.language is None:
            lang_rank = len(languages)  # untagged: probably the main language
        else:
            lang_rank = len(languages) + 1
        return (lang_rank, not s.sdh, str(s.path))

    found.sort(key=rank)
    # Only keep files in a preferred language, or untagged ones.
    return [s for s in found if rank(s)[0] <= len(languages)]
