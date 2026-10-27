"""FastAPI application: JSON API, webhooks, and the server-rendered web UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, auth
from ..db import create_all, load_settings, session_scope
from . import auth_routes, routes, webhooks

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
log = logging.getLogger(__name__)


def _authorize(request: Request) -> Response | None:
    """Attach the signed-in person to the request, or answer for it when not allowed."""
    path = request.url.path
    with session_scope() as session:
        settings = load_settings(session)
        if not auth.login_enabled(settings):
            request.state.principal = auth.OPEN
            return None
        person = auth.principal_from_token(session, request.cookies.get(auth.COOKIE), settings)
    request.state.principal = person or auth.Principal(name="", admin=False, method="none")
    if auth.is_public(path):
        return None
    api = path.startswith("/api/")
    if person is None:
        if api:
            return JSONResponse({"detail": "sign in first"}, status_code=401)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    if not person.admin and not auth.viewer_may_open(request.method, path):
        if api:
            return JSONResponse({"detail": "only an admin can do that"}, status_code=403)
        return RedirectResponse("/", status_code=303)
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_all()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Where Were We", version=__version__, lifespan=lifespan)
    app.include_router(routes.router)
    app.include_router(auth_routes.router)
    app.include_router(webhooks.router)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        denied = await run_in_threadpool(_authorize, request)
        return denied or await call_next(request)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def page(name: str, **ctx):
        def handler(request: Request) -> HTMLResponse:
            person = getattr(request.state, "principal", auth.OPEN)
            return templates.TemplateResponse(
                request,
                name,
                {
                    "version": __version__,
                    "path": request.url.path,
                    "person": person,
                    **ctx,
                    **request.path_params,
                },
            )

        return handler

    def login_page(request: Request, next: str = "/", error: str | None = None) -> Response:
        with session_scope() as session:
            settings = load_settings(session)
        if not auth.login_enabled(settings):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "version": __version__,
                "next": auth_routes.safe_next(next),
                "error": error,
                "plex": auth_routes.plex_available(settings),
            },
        )

    app.add_api_route("/login", login_page, methods=["GET"], include_in_schema=False)

    app.add_api_route("/", page("index.html"), methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/recaps/{recap_id:int}", page("recap.html"), methods=["GET"], include_in_schema=False
    )
    app.add_api_route(
        "/shows/{show_id:int}/characters",
        page("characters.html"),
        methods=["GET"],
        include_in_schema=False,
    )
    for name in ("library", "activity", "settings", "system"):
        app.add_api_route(
            f"/{name}", page(f"{name}.html"), methods=["GET"], include_in_schema=False
        )
    app.add_api_route(
        "/indexing",
        lambda: RedirectResponse("/library"),
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/favicon.ico",
        lambda: RedirectResponse("/static/icon.svg"),
        methods=["GET"],
        include_in_schema=False,
    )
    return app


app = create_app()
