"""Up Next: recommendations, a viewer's marks, pitches, artwork, and Add to Sonarr."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import Principal
from ..db import load_settings
from ..integrations.sonarr import SonarrError
from ..llm import prompts
from ..llm.client import LLMError, get_llm
from ..models import Pitch, Preference, Show, ShowMeta, TmdbTitle, User
from ..services import upnext
from ..services.meta import sonarr_client
from ..services.sync import plex_client
from ..sources.tmdb import IMAGES, TMDBClient
from ..worker import queue
from .deps import get_session
from .routes import _own, who

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)

# Sonarr's series list, so cards can say "In Sonarr" without asking it on every page load.
_SONARR_TTL = 300
_sonarr_cache: dict[str, tuple[float, tuple[set[int], set[int]]]] = {}
_sonarr_lock = threading.Lock()


def _sonarr_ids(settings) -> tuple[set[int], set[int]] | None:
    if not (settings.sonarr_url and settings.sonarr_api_key):
        return None
    key = settings.sonarr_url
    with _sonarr_lock:
        hit = _sonarr_cache.get(key)
        if hit and time.monotonic() - hit[0] < _SONARR_TTL:
            return hit[1]
    try:
        client = sonarr_client(settings)
        try:
            ids = client.series_ids()
        finally:
            client.close()
    except SonarrError as exc:
        log.warning("couldn't read Sonarr's series: %s", exc)
        ids = (set(), set())
    with _sonarr_lock:
        _sonarr_cache[key] = (time.monotonic(), ids)
    return ids


def _user(session: Session, person: Principal, user_id: int) -> User:
    user_id = _own(person, user_id)
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "viewer not found")
    return user


def _context(session: Session, person: Principal) -> upnext.Context:
    settings = load_settings(session)
    ctx = upnext.make_context(session, settings, _sonarr_ids(settings))
    ctx.names = person.admin
    return ctx


@router.get("/users/{user_id}/upnext")
def get_upnext(
    user_id: int,
    when: Literal["any", "quick", "weekend", "long"] = Query("any", alias="time"),
    story: Literal["any", "complete", "ongoing"] = "any",
    genre: str = "",
    with_user: int | None = None,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
) -> dict:
    user = _user(session, person, user_id)
    ctx = _context(session, person)
    together = with_user if person.admin else None  # viewers don't see others' progress
    data = upnext.up_next(ctx, user.id, upnext.Filters(when, story, genre), together)
    data["can_add"] = person.admin and data["status"]["sonarr"]
    return data


@router.get("/users/{user_id}/upnext/item")
def get_item(
    user_id: int,
    key: str,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
) -> dict:
    user = _user(session, person, user_id)
    ctx = _context(session, person)
    item = upnext.find_item(ctx, user.id, key)
    if item is None:
        raise HTTPException(404, "not found")
    return item


class Mark(BaseModel):
    key: str  # "show:12" or "tmdb:1396"
    kind: Literal["love", "nope", "seen", "later"]
    on: bool = True


def _subject(key: str) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"(show|tmdb):(\d+)", key)
    if not match:
        raise HTTPException(400, "key must look like show:12 or tmdb:1396")
    kind, value = match.group(1), int(match.group(2))
    return (value, None) if kind == "show" else (None, value)


@router.put("/users/{user_id}/preferences")
def mark(
    user_id: int,
    body: Mark,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
) -> dict:
    """Love, Not for me, Seen it, Save for later. Love and Not for me cancel each other."""
    user = _user(session, person, user_id)
    show_id, tmdb_id = _subject(body.key)
    if show_id is not None and session.get(Show, show_id) is None:
        raise HTTPException(404, "show not found")
    same = (
        (Preference.user_id == user.id)
        & (Preference.show_id == show_id if show_id else Preference.show_id.is_(None))
        & (Preference.tmdb_id == tmdb_id if tmdb_id else Preference.tmdb_id.is_(None))
    )
    opposite = {"love": ["nope"], "nope": ["love", "later"], "later": ["nope"]}.get(body.kind, [])
    if body.on:
        session.execute(delete(Preference).where(same, Preference.kind.in_(opposite)))
        if session.scalar(select(Preference).where(same, Preference.kind == body.kind)) is None:
            session.add(
                Preference(user_id=user.id, show_id=show_id, tmdb_id=tmdb_id, kind=body.kind)
            )
    else:
        session.execute(delete(Preference).where(same, Preference.kind == body.kind))
    session.commit()
    kinds = session.scalars(select(Preference.kind).where(same)).all()
    return {"key": body.key, "prefs": sorted(kinds)}


class PitchRequest(BaseModel):
    key: str
    fresh: bool = False


@router.post("/users/{user_id}/pitch")
def pitch(
    user_id: int,
    body: PitchRequest,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
) -> dict:
    """A short, spoiler-free "why you'd like it", written by the model once and kept."""
    user = _user(session, person, user_id)
    show_id, tmdb_id = _subject(body.key)
    saved = session.scalar(select(Pitch).where(Pitch.user_id == user.id, Pitch.subject == body.key))
    if saved is not None and not body.fresh:
        return {"key": body.key, "text": saved.text}
    ctx = _context(session, person)
    if show_id is not None:
        item = upnext.find_item(ctx, user.id, body.key)
        if item is None:
            raise HTTPException(404, "show not found")
    else:
        title = session.get(TmdbTitle, tmdb_id)
        if title is None:
            raise HTTPException(404, "show not found")
        lane = upnext.beyond_lane(ctx, ctx.profile(user.id), upnext.Filters())
        item = next((i for i in lane["items"] if i["key"] == body.key), None) or {
            "title": title.name,
            "overview": title.overview,
            "facts": {"genres": title.genres},
            "reasons": [],
        }
    name, premise, details, liked, reasons = upnext.pitch_material(ctx, user.id, item)
    if not premise:
        raise HTTPException(400, "there's no premise to pitch from yet")
    settings = ctx.settings
    session.commit()  # don't hold the database during the model call
    try:
        text = get_llm(settings).chat(
            [
                {"role": "system", "content": prompts.PITCH_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.pitch_user(name, premise, details, liked, reasons),
                },
            ],
            max_tokens=300,
        )
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    text = text.strip()
    if saved is None:
        saved = Pitch(user_id=user.id, subject=body.key, text=text, model=settings.llm_model)
        session.add(saved)
    else:
        saved.text, saved.model = text, settings.llm_model
    session.commit()
    return {"key": body.key, "text": text}


@router.post("/upnext/refresh")
def refresh(session: Session = Depends(get_session), person: Principal = Depends(who)) -> dict:
    """Re-read show details from Plex and TMDB now."""
    if not person.admin:
        raise HTTPException(403, "admins only")
    job = queue.enqueue(session, "sync_meta", {}, priority=5, dedupe_key="sync_meta")
    session.commit()
    return {"job_id": job.id if job else None}


class SonarrAdd(BaseModel):
    tmdb_id: int


@router.post("/sonarr/add")
def sonarr_add(
    body: SonarrAdd, session: Session = Depends(get_session), person: Principal = Depends(who)
) -> dict:
    if not person.admin:
        raise HTTPException(403, "admins only")
    settings = load_settings(session)
    if not (settings.sonarr_url and settings.sonarr_api_key):
        raise HTTPException(400, "Sonarr isn't set up (Settings → Sonarr)")
    tvdb_id = _tvdb_id(session, settings, body.tmdb_id)
    client = sonarr_client(settings)
    try:
        result = client.add(
            tvdb_id,
            quality_profile=settings.sonarr_quality_profile,
            root_folder=settings.sonarr_root_folder,
            search=settings.sonarr_search,
        )
    except SonarrError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        client.close()
    _sonarr_cache.clear()
    log.info("added %s to Sonarr (tmdb %s)", result["title"], body.tmdb_id)
    return result


def _tvdb_id(session: Session, settings, tmdb_id: int) -> int:
    """Sonarr looks shows up by TVDB id; TMDB knows it."""
    meta = session.scalar(
        select(ShowMeta).join(Show, Show.id == ShowMeta.show_id).where(Show.tmdb_id == tmdb_id)
    )
    if meta is not None and meta.tvdb_id:
        return meta.tvdb_id
    if not settings.tmdb_api_key:
        raise HTTPException(400, "a TMDB key is needed to find the show for Sonarr")
    client = TMDBClient(settings.tmdb_api_key)
    try:
        data = client.show(tmdb_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"TMDB lookup failed: {exc}") from exc
    finally:
        client.close()
    if not data or not data["tvdb_id"]:
        raise HTTPException(404, "TMDB doesn't know this show's TVDB id, which Sonarr needs")
    return int(data["tvdb_id"])


# ---- artwork ---------------------------------------------------------------------------

_IMAGE_PATH = re.compile(r"^/[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)$")
_SIZES = {"w185", "w342", "w500", "w780", "w1280"}


def _cached(settings, folder: str, name: str, fetch) -> Response:
    cache = settings.cache_dir / folder
    path = cache / (hashlib.sha1(name.encode()).hexdigest() + ".img")
    if not path.exists():
        try:
            content = fetch()
        except Exception as exc:
            raise HTTPException(502, f"could not fetch image: {exc}") from exc
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return Response(
        path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/tmdb/image/{size}/{name}")
def tmdb_image(size: str, name: str, session: Session = Depends(get_session)) -> Response:
    """TMDB posters and backdrops, fetched through this server and cached."""
    if size not in _SIZES or not _IMAGE_PATH.match(f"/{name}"):
        raise HTTPException(404, "no such image")
    settings = load_settings(session)

    def fetch() -> bytes:
        resp = httpx.get(f"{IMAGES}/{size}/{name}", timeout=20)
        resp.raise_for_status()
        return resp.content

    return _cached(settings, "tmdb", f"{size}/{name}", fetch)


@router.get("/shows/{show_id}/art")
def show_art(show_id: int, session: Session = Depends(get_session)) -> Response:
    """The show's backdrop from Plex, sized for the page and cached."""
    meta = session.get(ShowMeta, show_id)
    if meta is None or not meta.art_url:
        raise HTTPException(404, "no artwork")
    settings = load_settings(session)

    def fetch() -> bytes:
        client = plex_client(settings)
        try:
            content, _ = client.image(meta.art_url, width=1280, height=720)
            return content
        finally:
            client.close()

    return _cached(settings, "art", meta.art_url, fetch)
