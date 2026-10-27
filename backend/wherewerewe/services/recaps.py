"""Creating, generating, and delivering recaps."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import load_settings, session_scope
from ..integrations import notifiers
from ..llm import prompts
from ..llm.client import LLM, get_llm
from ..llm.recap import LENGTHS, build_context, write_recap
from ..llm.verify import VerificationResult, verify_recap
from ..models import Episode, Recap, Show, User
from ..worker import queue
from .indexing import index_episode
from .library import group_label, group_members
from .progress import boundary_episode

log = logging.getLogger(__name__)

REGENERATE_IF_REMOVED = 0.4
STRICT_RETRY = (
    "Your previous draft included statements the source material does not support. "
    "Use only names and events that appear in the source material above."
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class RecapRequestError(ValueError):
    pass


def resolve_boundary(
    session: Session, *, user_id: int | None, show_id: int, boundary_episode_id: int | None
) -> Episode:
    user_boundary = boundary_episode(session, user_id, show_id) if user_id else None
    if boundary_episode_id is None:
        if user_id is None:
            raise RecapRequestError("a boundary episode is required when no user is given")
        if user_boundary is None:
            raise RecapRequestError("this user hasn't finished an episode of this show yet")
        return user_boundary
    ep = session.get(Episode, boundary_episode_id)
    if ep is None or ep.show_id != show_id:
        raise RecapRequestError("boundary episode not found in this show")
    if ep.season == 0:
        raise RecapRequestError("specials can't be used as a recap boundary")
    if user_id is not None and (user_boundary is None or ep.order_key > user_boundary.order_key):
        raise RecapRequestError(
            "that episode is past where this user has watched; refusing to spoil it"
        )
    return ep


def create_recap(
    session: Session,
    settings: Settings,
    *,
    show_id: int,
    length: str,
    user_id: int | None = None,
    boundary_episode_id: int | None = None,
    trigger: str = "manual",
    fresh: bool = False,
    enqueue: bool = True,
) -> Recap:
    if length not in LENGTHS:
        raise RecapRequestError(f"length must be one of {', '.join(LENGTHS)}")
    if session.get(Show, show_id) is None:
        raise RecapRequestError("show not found")
    if user_id is not None and session.get(User, user_id) is None:
        raise RecapRequestError("user not found")
    boundary = resolve_boundary(
        session, user_id=user_id, show_id=show_id, boundary_episode_id=boundary_episode_id
    )
    if not fresh:
        cached = session.scalar(
            select(Recap)
            .where(
                Recap.show_id == show_id,
                Recap.boundary_episode_id == boundary.id,
                Recap.length == length,
                Recap.status.in_(("queued", "generating", "verifying", "done")),
                Recap.user_id.is_(None) if user_id is None else Recap.user_id == user_id,
                Recap.created_at > _now() - timedelta(hours=settings.recap_cache_hours),
            )
            .order_by(Recap.id.desc())
            .limit(1)
        )
        if cached is not None:
            return cached
    recap = Recap(
        user_id=user_id,
        show_id=show_id,
        boundary_episode_id=boundary.id,
        length=length,
        trigger=trigger,
        status="queued",
    )
    session.add(recap)
    session.flush()
    if enqueue:
        queue.enqueue(
            session,
            "generate_recap",
            {"recap_id": recap.id},
            priority=50,
            dedupe_key=f"generate_recap:{recap.id}",
        )
    return recap


def _set(recap_id: int, **values) -> None:
    with session_scope() as s:
        s.execute(update(Recap).where(Recap.id == recap_id).values(**values))


def run_recap(
    recap_id: int,
    *,
    llm: LLM | None = None,
    verify_llm: LLM | None = None,
    notify: bool = False,
    verify: bool | None = None,
) -> Recap:
    """Generate a recap, streaming the draft into the database as it is written."""
    with session_scope() as session:
        settings = load_settings(session)
        recap = session.get(Recap, recap_id)
        if recap is None:
            raise LookupError(f"recap {recap_id} not found")
        show = session.get(Show, recap.show_id)
        boundary = session.get(Episode, recap.boundary_episode_id)
        assert show is not None and boundary is not None
        llm = llm or get_llm(settings)
        ctx = build_context(session, show, boundary, recap.length, settings, llm.context_tokens)
        # Make sure the latest watched episodes are summarized; they matter most.
        recent_missing = ctx.coverage["missing"][-settings.recap_detail_episodes :]
        if recent_missing:
            _set(recap_id, status="generating", draft_text="")
            for ep in session.scalars(
                select(Episode).where(
                    Episode.show_id == show.id, Episode.part_of_episode_id.is_(None)
                )
            ):
                if group_label(group_members(session, ep)) in recent_missing:
                    try:
                        index_episode(session, settings, ep.id, llm=llm)
                        session.commit()
                    except Exception as exc:
                        session.rollback()
                        log.warning("could not index %s for recap: %s", ep.code, exc)
            ctx = build_context(session, show, boundary, recap.length, settings, llm.context_tokens)
        if ctx.coverage["summarized"] == 0:
            raise RuntimeError(
                "none of the watched episodes are summarized yet; index the show first"
            )
        length, context_text = recap.length, ctx.text
        user = session.get(User, recap.user_id) if recap.user_id else None
        notify_target = user.notify_target if user else None
        notify_enabled = user.notifications_enabled if user else True
        show_title = show.title

    _set(
        recap_id,
        status="generating",
        draft_text="",
        coverage_json=ctx.coverage,
        model=llm.model,
        prompt_version=prompts.RECAP_VERSION,
        error=None,
    )
    last_write = 0.0

    def on_text(text: str) -> None:
        nonlocal last_write
        if time.monotonic() - last_write >= 0.4:
            last_write = time.monotonic()
            _set(recap_id, draft_text=text)

    draft = write_recap(llm, context_text, length, on_text)
    _set(recap_id, draft_text=draft)
    final_text, verified, verification = draft, False, None
    do_verify = settings.recap_verify if verify is None else verify
    if do_verify:
        _set(recap_id, status="verifying")
        verify_llm = verify_llm or get_llm(settings, verify=True)
        result = verify_recap(verify_llm, context_text, draft)
        if result.removed_ratio > REGENERATE_IF_REMOVED and len(result.removed) >= 2:
            log.info(
                "recap %s: %d of %d sentences unsupported; regenerating once",
                recap_id,
                len(result.removed),
                result.checked,
            )
            _set(recap_id, status="generating")
            second = write_recap(llm, context_text, length, None, STRICT_RETRY)
            _set(recap_id, status="verifying", draft_text=second)
            retry: VerificationResult = verify_recap(verify_llm, context_text, second)
            if retry.removed_ratio < result.removed_ratio:
                result = retry
        final_text, verified, verification = result.text, result.llm_checked, result.as_json()
    if not final_text.strip():
        _set(
            recap_id,
            status="failed",
            verification_json=verification,
            finished_at=_now(),
            error="Every sentence of the recap failed verification against the episode "
            "summaries, so nothing was kept. Try again or use a larger model.",
        )
    else:
        _set(
            recap_id,
            status="done",
            text=final_text,
            verified=verified,
            verification_json=verification,
            finished_at=_now(),
        )

    with session_scope() as session:
        recap = session.get(Recap, recap_id)
        assert recap is not None
        if notify and recap.status == "done" and notify_enabled:
            deliver(settings, recap, show_title, notify_target)
        session.expunge(recap)
        return recap


def deliver(settings: Settings, recap: Recap, show_title: str, user_target: str | None) -> None:
    link = f"{settings.public_url.rstrip('/')}/recaps/{recap.id}"
    for target in notifiers.targets_for(user_target, settings):
        try:
            channel = notifiers.send(
                target, f"Previously on {show_title}", recap.text or "", link, settings
            )
            log.info("recap %s sent via %s", recap.id, channel)
        except Exception as exc:
            log.warning("notification for recap %s failed: %s", recap.id, exc)
