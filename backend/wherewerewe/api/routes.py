"""JSON API."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..auth import OPEN, Principal, hash_password
from ..config import ALL_RUNTIME_KEYS, RUNTIME_KEYS, SECRET_KEYS, Settings, parse_setting
from ..db import load_settings, new_session
from ..llm import prompts
from ..llm.client import LLMError, get_llm
from ..llm.ledger import ledger_as_of, ledger_notes
from ..models import Episode, Job, Recap, Setting, Show, User, WatchProgress
from ..services.indexing import actors_as_of, played_by, show_stats
from ..services.library import episode_by_code, regular_episodes
from ..services.progress import boundary_episode, shows_in_progress
from ..services.recaps import RecapRequestError, create_recap
from ..services.sync import plex_client, tautulli_client
from ..sources import extract, whisper
from ..worker import queue
from .deps import get_session

router = APIRouter(prefix="/api")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def who(request: Request) -> Principal:
    """The signed-in person (set by the login middleware); everyone is admin with login off."""
    return getattr(request.state, "principal", OPEN)


def _own(person: Principal, user_id: int | None) -> int | None:
    """Viewers only ever act as themselves. Returns the user id to use."""
    if person.admin:
        return user_id
    if user_id is not None and user_id != person.user_id:
        raise HTTPException(403, "that belongs to another viewer")
    return person.user_id


# -- users -------------------------------------------------------------------------------


def user_json(user: User, settings: Settings) -> dict:
    return {
        "id": user.id,
        "plex_user_id": user.plex_user_id,
        "display_name": user.display_name,
        "username": user.username,
        "notify_target": user.notify_target,
        "notifications_enabled": user.notifications_enabled,
        "gap_threshold_days": user.gap_threshold_days,
        "effective_gap_days": user.gap_threshold_days or settings.default_gap_days,
    }


@router.get("/users")
def list_users(
    session: Session = Depends(get_session), person: Principal = Depends(who)
) -> list[dict]:
    settings = load_settings(session)
    query = select(User).order_by(func.lower(User.display_name))
    if not person.admin:
        query = query.where(User.id == person.user_id)
    return [user_json(u, settings) for u in session.scalars(query)]


class UserUpdate(BaseModel):
    display_name: str | None = None
    notify_target: str | None = None
    notifications_enabled: bool | None = None
    gap_threshold_days: int | None = Field(default=None, ge=0, le=3650)


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdate,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
) -> dict:
    _own(person, user_id)
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    for key in body.model_fields_set:
        value = getattr(body, key)
        if key in ("notify_target",) and isinstance(value, str):
            value = value.strip() or None
        if key == "gap_threshold_days" and value == 0:
            value = None
        setattr(user, key, value)
    session.commit()
    return user_json(user, load_settings(session))


@router.get("/users/{user_id}/shows")
def user_shows(
    user_id: int, session: Session = Depends(get_session), person: Principal = Depends(who)
) -> list[dict]:
    _own(person, user_id)
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    return shows_in_progress(session, user, _now())


# -- shows -------------------------------------------------------------------------------


def _show_json(session: Session, show: Show, settings: Settings | None = None) -> dict:
    return {
        "id": show.id,
        "title": show.title,
        "year": show.year,
        "tmdb_id": show.tmdb_id,
        "has_poster": bool(show.poster_url),
        "source": "folder" if show.local_path else "plex",
        "stats": show_stats(session, show.id, settings or load_settings(session)),
    }


@router.get("/shows")
def list_shows(session: Session = Depends(get_session), watched_only: bool = False) -> list[dict]:
    query = select(Show).order_by(func.lower(Show.title))
    if watched_only:
        query = query.where(Show.id.in_(select(WatchProgress.show_id)))
    settings = load_settings(session)
    return [_show_json(session, s, settings) for s in session.scalars(query)]


@router.get("/shows/{show_id}")
def get_show(
    show_id: int, user: int | None = None, session: Session = Depends(get_session)
) -> dict:
    show = session.get(Show, show_id)
    if show is None:
        raise HTTPException(404, "show not found")
    data = _show_json(session, show)
    if user is not None:
        b = boundary_episode(session, user, show_id)
        data["boundary"] = {"id": b.id, "code": b.code, "title": b.title} if b else None
    return data


@router.get("/shows/{show_id}/episodes")
def show_episodes(
    show_id: int, user: int | None = None, session: Session = Depends(get_session)
) -> list[dict]:
    """Episodes a recap can stop at. With a user, only episodes they have reached."""
    if session.get(Show, show_id) is None:
        raise HTTPException(404, "show not found")
    episodes = regular_episodes(session, show_id)
    if user is not None:
        b = boundary_episode(session, user, show_id)
        episodes = [e for e in episodes if b is not None and e.order_key <= b.order_key]
    return [{"id": e.id, "code": e.code, "title": e.title} for e in episodes]


@router.get("/shows/{show_id}/poster")
def show_poster(show_id: int, session: Session = Depends(get_session)) -> Response:
    show = session.get(Show, show_id)
    if show is None or not show.poster_url:
        raise HTTPException(404, "no poster")
    settings = load_settings(session)
    cache = settings.cache_dir / "posters"
    name = hashlib.sha1(show.poster_url.encode()).hexdigest() + ".img"
    path = cache / name
    if not path.exists():
        try:
            client = plex_client(settings)
            try:
                content, _ctype = client.image(show.poster_url, width=300)
            finally:
                client.close()
        except Exception as exc:
            raise HTTPException(502, f"could not fetch poster: {exc}") from exc
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return Response(
        path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _resolve_user_boundary(
    session: Session, show_id: int, user: int | None, until: str | None
) -> tuple[int, int]:
    if until:
        ep = episode_by_code(session, show_id, until)
        if user is not None:
            b = boundary_episode(session, user, show_id)
            if b is None or ep.order_key > b.order_key:
                raise HTTPException(403, "that episode is past where this user has watched")
        return ep.order_key
    if user is None:
        raise HTTPException(400, "pass ?user= or ?until=SxxEyy")
    b = boundary_episode(session, user, show_id)
    if b is None:
        raise HTTPException(404, "this user hasn't finished an episode of this show")
    return b.order_key


@router.get("/shows/{show_id}/characters")
def characters(
    show_id: int,
    user: int | None = None,
    until: str | None = None,
    q: str | None = None,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
) -> dict:
    """The character ledger as of the user's position (never later)."""
    user = _own(person, user)
    if session.get(Show, show_id) is None:
        raise HTTPException(404, "show not found")
    try:
        boundary = _resolve_user_boundary(session, show_id, user, until)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    entries = ledger_as_of(session, show_id, boundary)
    if q:
        needle = q.lower()
        entries = [
            e
            for e in entries
            if needle in e.name.lower() or any(needle in a.lower() for a in e.aliases)
        ]
    actors = actors_as_of(session, show_id, boundary)
    return {
        "as_of": f"S{boundary[0]:02d}E{boundary[1]:02d}",
        "characters": [{**e.__dict__, "actor": played_by(e, actors)} for e in entries],
    }


class WhoisRequest(BaseModel):
    character_id: int
    user: int | None = None
    until: str | None = None
    question: str | None = None


@router.post("/shows/{show_id}/whois")
def whois(
    show_id: int,
    body: WhoisRequest,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
) -> dict:
    """Answer "who is X again?" from the ledger as of the user's position."""
    body.user = _own(person, body.user)
    if session.get(Show, show_id) is None:
        raise HTTPException(404, "show not found")
    try:
        boundary = _resolve_user_boundary(session, show_id, body.user, body.until)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    entry = next(
        (
            e
            for e in ledger_as_of(session, show_id, boundary)
            if e.character_id == body.character_id
        ),
        None,
    )
    if entry is None:
        raise HTTPException(404, "character not seen yet at this point")
    settings = load_settings(session)
    question = body.question or f"Who is {entry.name} again?"
    try:
        answer = get_llm(settings).chat(
            [
                {"role": "system", "content": prompts.WHOIS_SYSTEM},
                {"role": "user", "content": prompts.whois_user(question, ledger_notes(entry))},
            ],
            max_tokens=400,
        )
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "character_id": entry.character_id,
        "name": entry.name,
        "question": question,
        "answer": answer.strip(),
        "as_of": f"S{boundary[0]:02d}E{boundary[1]:02d}",
    }


class IndexRequest(BaseModel):
    scope: Literal["all", "watched"] = "all"
    force: bool = False


@router.post("/shows/{show_id}/index")
def index_show(
    show_id: int, body: IndexRequest | None = None, session: Session = Depends(get_session)
) -> dict:
    if session.get(Show, show_id) is None:
        raise HTTPException(404, "show not found")
    body = body or IndexRequest()
    job = queue.enqueue(
        session,
        "index_show",
        {"show_id": show_id, "scope": body.scope, "force": body.force},
        priority=3,
        dedupe_key=None if body.force else f"index_show:{show_id}:{body.scope}",
    )
    session.commit()
    return {"job_id": job.id if job else None}


# -- recaps ------------------------------------------------------------------------------


class RecapRequest(BaseModel):
    show_id: int
    user_id: int | None = None
    length: Literal["quick", "standard", "deep"] = "standard"
    until: str | None = Field(default=None, description="Episode code to stop at, e.g. S02E04")
    fresh: bool = False


def recap_json(session: Session, recap: Recap) -> dict:
    show = session.get(Show, recap.show_id)
    boundary = session.get(Episode, recap.boundary_episode_id)
    return {
        "id": recap.id,
        "show_id": recap.show_id,
        "show_title": show.title if show else None,
        "user_id": recap.user_id,
        "boundary": boundary.code if boundary else None,
        "boundary_title": boundary.title if boundary else None,
        "length": recap.length,
        "status": recap.status,
        "text": recap.text if recap.status == "done" else None,
        "draft_text": recap.draft_text,
        "verified": recap.verified,
        "verification": recap.verification_json,
        "coverage": recap.coverage_json,
        "trigger": recap.trigger,
        "model": recap.model,
        "error": recap.error,
        "created_at": _iso(recap.created_at),
        "finished_at": _iso(recap.finished_at),
    }


@router.post("/recaps", status_code=201)
def post_recap(
    body: RecapRequest, session: Session = Depends(get_session), person: Principal = Depends(who)
) -> dict:
    body.user_id = _own(person, body.user_id)
    settings = load_settings(session)
    boundary_id = None
    if body.until:
        try:
            boundary_id = episode_by_code(session, body.show_id, body.until).id
        except (LookupError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        recap = create_recap(
            session,
            settings,
            show_id=body.show_id,
            length=body.length,
            user_id=body.user_id,
            boundary_episode_id=boundary_id,
            fresh=body.fresh,
        )
    except RecapRequestError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return recap_json(session, recap)


@router.get("/recaps")
def list_recaps(
    user: int | None = None,
    show: int | None = None,
    limit: int = 20,
    session: Session = Depends(get_session),
) -> list[dict]:
    query = select(Recap).order_by(Recap.id.desc()).limit(min(limit, 100))
    if user is not None:
        query = query.where(Recap.user_id == user)
    if show is not None:
        query = query.where(Recap.show_id == show)
    return [recap_json(session, r) for r in session.scalars(query)]


def _recap_snapshot(recap_id: int) -> dict | None:
    session = new_session()
    try:
        recap = session.get(Recap, recap_id)
        return recap_json(session, recap) if recap else None
    finally:
        session.close()


async def _recap_events(recap_id: int, request: Request):
    sent_draft = ""
    last_status = None
    deadline = asyncio.get_running_loop().time() + 1800
    while asyncio.get_running_loop().time() < deadline:
        if await request.is_disconnected():
            return
        snap = await run_in_threadpool(_recap_snapshot, recap_id)
        if snap is None:
            yield f"event: error\ndata: {json.dumps({'error': 'not found'})}\n\n"
            return
        if snap["status"] != last_status:
            last_status = snap["status"]
            yield f"event: status\ndata: {json.dumps({'status': last_status})}\n\n"
        draft = snap["draft_text"] or ""
        if draft != sent_draft:
            if draft.startswith(sent_draft):
                payload = {"delta": draft[len(sent_draft) :]}
            else:
                payload = {"replace": draft}
            sent_draft = draft
            yield f"event: draft\ndata: {json.dumps(payload)}\n\n"
        if snap["status"] in ("done", "failed"):
            yield f"event: final\ndata: {json.dumps(snap)}\n\n"
            return
        await asyncio.sleep(0.3)
    yield f"event: error\ndata: {json.dumps({'error': 'timed out'})}\n\n"


@router.get("/recaps/{recap_id}")
def get_recap(
    recap_id: int,
    request: Request,
    stream: bool = False,
    session: Session = Depends(get_session),
    person: Principal = Depends(who),
):
    """Fetch a recap. With ?stream=true or Accept: text/event-stream, stream it as SSE."""
    recap = session.get(Recap, recap_id)
    if recap is None or (not person.admin and recap.user_id != person.user_id):
        raise HTTPException(404, "recap not found")
    if stream or "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            _recap_events(recap_id, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return recap_json(session, recap)


# -- jobs --------------------------------------------------------------------------------


def job_json(job: Job) -> dict:
    return {
        "id": job.id,
        "type": job.type,
        "queue": job.queue,
        "status": job.status,
        "payload": job.payload,
        "priority": job.priority,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "message": job.message,
        "error": job.error,
        "timings": job.timings,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "run_after": _iso(job.run_after),
    }


@router.get("/jobs")
def list_jobs(
    status: str | None = None,
    limit: int = Query(50, le=500),
    session: Session = Depends(get_session),
) -> dict:
    counts = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    query = select(Job).order_by(Job.id.desc()).limit(limit)
    if status:
        query = query.where(Job.status == status)
    return {
        "counts": counts,
        "jobs": [
            {**job_json(j), "subject": _job_subject(session, j)} for j in session.scalars(query)
        ],
    }


def _job_subject(session: Session, job: Job) -> str | None:
    """What a job is about, for people: "The Office S09E21" rather than an episode id."""
    payload = job.payload or {}
    if "episode_id" in payload:
        ep = session.get(Episode, payload["episode_id"])
        show = session.get(Show, ep.show_id) if ep else None
        return f"{show.title} {ep.code}" if ep and show else None
    if "recap_id" in payload:
        recap = session.get(Recap, payload["recap_id"])
        show = session.get(Show, recap.show_id) if recap else None
        return show.title if show else None
    if "show_id" in payload:
        show = session.get(Show, payload["show_id"])
        if show is None:
            return None
        return f"{show.title} season {payload['season']}" if "season" in payload else show.title
    return None


@router.post("/jobs/cancel")
def cancel_jobs(lane: str | None = None, session: Session = Depends(get_session)) -> dict:
    """Cancel queued jobs, e.g. lane=index to stop pending indexing."""
    count = queue.cancel_queued(session, lane)
    session.commit()
    return {"cancelled": count}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, session: Session = Depends(get_session)) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status != "failed":
        raise HTTPException(409, "only failed jobs can be retried")
    job.status, job.attempts, job.error, job.run_after = "queued", 0, None, _now()
    session.commit()
    return job_json(job)


class SyncRequest(BaseModel):
    library: bool = True
    history: bool = True
    full: bool = False


@router.post("/sync")
def trigger_sync(body: SyncRequest | None = None, session: Session = Depends(get_session)) -> dict:
    body = body or SyncRequest()
    ids = []
    if body.library:
        job = queue.enqueue(session, "sync_library", {}, priority=5, dedupe_key="sync_library")
        ids.append(job.id if job else None)
    if body.history:
        job = queue.enqueue(
            session, "sync_history", {"full": body.full}, priority=5, dedupe_key="sync_history:"
        )
        ids.append(job.id if job else None)
    session.commit()
    return {"job_ids": ids}


# -- settings ----------------------------------------------------------------------------


def _mask(key: str, value) -> object:
    if key in SECRET_KEYS and value:
        return "********"
    return value


@router.get("/settings")
def get_settings(session: Session = Depends(get_session)) -> dict:
    settings = load_settings(session)
    overridden = set(session.scalars(select(Setting.key).where(Setting.key.in_(ALL_RUNTIME_KEYS))))
    groups = []
    for group, keys in RUNTIME_KEYS.items():
        groups.append(
            {
                "name": group,
                "fields": [
                    {
                        "key": k,
                        "value": _mask(k, getattr(settings, k)),
                        "secret": k in SECRET_KEYS,
                        "type": Settings.model_fields[k].annotation.__name__
                        if hasattr(Settings.model_fields[k].annotation, "__name__")
                        else "str",
                        "overridden": k in overridden,
                    }
                    for k in keys
                ],
            }
        )
    return {"groups": groups}


@router.put("/settings")
def put_settings(
    values: dict[str, str | int | float | bool | None], session: Session = Depends(get_session)
) -> dict:
    """Save overrides. A null value removes the override (back to the environment value)."""
    errors = {}
    for key, raw in values.items():
        if key not in ALL_RUNTIME_KEYS:
            errors[key] = "not a runtime setting"
            continue
        if raw == "********":
            continue  # masked secret left unchanged
        row = session.get(Setting, key)
        if raw is None:
            if row is not None:
                session.delete(row)
            continue
        try:
            parsed = parse_setting(key, raw)
        except Exception as exc:
            errors[key] = str(exc).splitlines()[0]
            continue
        stored = str(parsed).lower() if isinstance(parsed, bool) else str(parsed)
        if key == "admin_password":
            stored = hash_password(stored)
        if row is None:
            session.add(Setting(key=key, value=stored))
        else:
            row.value = stored
    if errors:
        session.rollback()
        raise HTTPException(422, {"errors": errors})
    session.commit()
    return get_settings(session)


@router.get("/llm/models")
def llm_models(session: Session = Depends(get_session)) -> dict:
    settings = load_settings(session)
    try:
        return {"models": get_llm(settings).list_models(), "current": settings.llm_model}
    except Exception as exc:
        raise HTTPException(502, f"could not list models: {exc}") from exc


# -- health ------------------------------------------------------------------------------


def _check(fn) -> dict:
    try:
        return {"ok": True, "detail": fn()}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    """Connectivity to Plex, Tautulli and the LLM, plus local tooling."""
    settings = load_settings(session)
    session.execute(text("SELECT 1"))
    checks: dict[str, dict] = {"database": {"ok": True, "detail": "ok"}}

    def plex():
        if not settings.plex_url:
            raise RuntimeError("not configured")
        c = plex_client(settings)
        try:
            return c.check()
        finally:
            c.close()

    def tautulli():
        if not settings.tautulli_url:
            raise RuntimeError("not configured")
        c = tautulli_client(settings)
        try:
            return c.check()
        finally:
            c.close()

    checks["plex"] = _check(plex)
    checks["tautulli"] = _check(tautulli)
    checks["llm"] = _check(lambda: get_llm(settings).ping())
    checks["ffmpeg"] = {
        "ok": extract.ffmpeg_available(),
        "detail": "ok" if extract.ffmpeg_available() else "not installed",
    }
    checks["whisper"] = {
        "ok": True,
        "detail": (
            "disabled"
            if not settings.whisper_enabled
            else "installed"
            if whisper.whisper_installed()
            else "not installed (episodes without subtitles will use the synopsis or be skipped)"
        ),
    }
    worker_seen = session.scalar(select(func.max(Job.started_at)))
    checks["queue"] = {"ok": True, "detail": f"last job started {_iso(worker_seen) or 'never'}"}
    return {
        "ok": all(
            c["ok"] for k, c in checks.items() if k in ("database", "plex", "tautulli", "llm")
        ),
        "checks": checks,
    }


@router.get("/health/live")
def live() -> dict:
    return {"ok": True}
