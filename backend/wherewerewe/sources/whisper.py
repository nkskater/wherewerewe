"""Speech-to-text fallback with faster-whisper (optional dependency)."""

from __future__ import annotations

import importlib.util
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_model = None
_model_key: tuple[str, str, str] | None = None
_lock = threading.Lock()


def whisper_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _get_model(name: str, device: str, compute_type: str):
    global _model, _model_key
    key = (name, device, compute_type)
    if _model is None or _model_key != key:
        from faster_whisper import WhisperModel

        log.info("loading whisper model %s on %s (%s)", name, device, compute_type)
        _model = WhisperModel(name, device=device, compute_type=compute_type)
        _model_key = key
    return _model


def transcribe(
    wav: Path, model: str, device: str, compute_type: str = "", language: str | None = None
) -> str:
    """Transcribe a WAV file, returning one segment per line."""
    if not whisper_installed():
        raise RuntimeError("faster-whisper is not installed (pip install 'wherewerewe[whisper]')")
    compute_type = compute_type or ("float16" if device == "cuda" else "int8")
    with _lock:  # one transcription at a time per process; the GPU is the bottleneck anyway
        whisper = _get_model(model, device, compute_type)
        segments, _info = whisper.transcribe(str(wav), language=language, vad_filter=True)
        return "\n".join(seg.text.strip() for seg in segments if seg.text.strip())
