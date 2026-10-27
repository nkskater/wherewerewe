"""Translate file paths as Plex sees them into paths inside this container."""

from __future__ import annotations

from pathlib import Path


def parse_path_map(raw: str) -> list[tuple[str, str]]:
    """Parse "plex/prefix=local/prefix;other=other" into (plex, local) pairs, longest first."""
    pairs: list[tuple[str, str]] = []
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        plex, local = chunk.split("=", 1)
        plex, local = plex.strip().rstrip("/"), local.strip().rstrip("/")
        if plex and local:
            pairs.append((plex, local))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def map_path(plex_path: str | None, raw_map: str) -> Path | None:
    """Return the local path for a Plex file path, or None if it can't be found."""
    if not plex_path:
        return None
    for plex_prefix, local_prefix in parse_path_map(raw_map):
        if plex_path == plex_prefix or plex_path.startswith(plex_prefix + "/"):
            candidate = Path(local_prefix + plex_path[len(plex_prefix) :])
            if candidate.exists():
                return candidate
    direct = Path(plex_path)
    return direct if direct.exists() else None
