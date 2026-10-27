"""Sign-in endpoints: the admin password and Sign in with Plex."""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    COOKIE,
    PIN_COOKIE,
    PIN_SECONDS,
    SESSION_SECONDS,
    Principal,
    client_id,
    login_enabled,
    session_token,
    sign,
    unsign,
    verify_password,
)
from ..db import load_settings
from ..integrations import plextv
from ..models import User
from ..services.sync import plex_client, upsert_user
from .deps import get_session
from .routes import who

router = APIRouter()
log = logging.getLogger(__name__)


def plex_tv(session: Session) -> plextv.PlexTV:
    return plextv.PlexTV(client_id(session))


def plex_available(settings) -> bool:
    return bool(settings.plex_login and settings.plex_url)


def safe_next(target: str | None) -> str:
    """Only same-site paths, so the login can't be used to bounce people elsewhere."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


def _set_cookie(response: Response, request: Request, name: str, value: str, max_age: int):
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.get("/api/me")
def me(
    request: Request, session: Session = Depends(get_session), person: Principal = Depends(who)
) -> dict:
    settings = load_settings(session)
    return {
        "login": login_enabled(settings),
        "name": person.name,
        "admin": person.admin,
        "user_id": person.user_id,
        "method": person.method,
    }


class PasswordLogin(BaseModel):
    password: str
    next: str | None = None


@router.post("/auth/password")
def password_login(
    body: PasswordLogin,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    settings = load_settings(session)
    if not login_enabled(settings):
        raise HTTPException(400, "login is off; set an admin password first")
    if not verify_password(settings.admin_password, body.password):
        time.sleep(1)  # slow down guessing
        raise HTTPException(401, "wrong password")
    principal = Principal(name="Admin", admin=True, method="password")
    _set_cookie(
        response, request, COOKIE, session_token(session, principal, settings), SESSION_SECONDS
    )
    return {"next": safe_next(body.next)}


class PlexStart(BaseModel):
    next: str | None = None


@router.post("/auth/plex/start")
def plex_start(
    body: PlexStart, request: Request, response: Response, session: Session = Depends(get_session)
) -> dict:
    settings = load_settings(session)
    if not (login_enabled(settings) and plex_available(settings)):
        raise HTTPException(400, "Plex sign-in is off")
    tv = plex_tv(session)
    try:
        pin_id, code = tv.create_pin()
    except plextv.PlexTVError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        tv.close()
    forward = str(request.base_url).rstrip("/") + "/auth/plex/callback"
    pin = sign(session, {"pin": pin_id, "code": code, "next": safe_next(body.next)}, PIN_SECONDS)
    _set_cookie(response, request, PIN_COOKIE, pin, PIN_SECONDS)
    return {"url": tv.auth_url(code, forward)}


def _fail(message: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={quote(message)}", status_code=303)


@router.get("/auth/plex/callback")
def plex_callback(request: Request, session: Session = Depends(get_session)) -> Response:
    settings = load_settings(session)
    pin = unsign(session, request.cookies.get(PIN_COOKIE))
    if not (login_enabled(settings) and plex_available(settings)):
        return _fail("Plex sign-in is off")
    if pin is None:
        return _fail("That sign-in took too long or came from another browser. Try again.")
    tv = plex_tv(session)
    try:
        token = None
        for _ in range(5):  # Plex can take a moment to attach the token
            token = tv.pin_token(int(pin["pin"]), str(pin["code"]))
            if token:
                break
            time.sleep(1)
        if not token:
            return _fail("Plex sign-in wasn't completed.")
        account = tv.account(token)
        plex = plex_client(settings)
        try:
            machine_id = plex.identity()
        finally:
            plex.close()
        access = tv.server_access(token, machine_id)
    except Exception as exc:  # plex.tv or the local server unreachable
        log.warning("Plex sign-in failed: %s", exc)
        return _fail(f"Couldn't finish signing in with Plex: {exc}")
    finally:
        tv.close()
    if not access.allowed:
        return _fail(f"The Plex account {account.username or account.title} can't use this server.")

    # Keep the name Tautulli gave an existing viewer; only new viewers take their Plex name.
    user = session.scalar(select(User).where(User.plex_user_id == account.id))
    if user is None:
        user = upsert_user(session, account.id, account.title, account.username or None)
    session.commit()
    principal = Principal(name=account.title, admin=access.owner, user_id=user.id, method="plex")
    response = RedirectResponse(str(pin.get("next") or "/"), status_code=303)
    _set_cookie(
        response, request, COOKIE, session_token(session, principal, settings), SESSION_SECONDS
    )
    response.delete_cookie(PIN_COOKIE, path="/")
    log.info("%s signed in with Plex (%s)", account.title, "owner" if access.owner else "viewer")
    return response


@router.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
