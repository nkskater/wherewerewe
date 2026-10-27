"""FastAPI application: JSON API, webhooks, and the server-rendered web UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..db import create_all
from . import routes, webhooks

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_all()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Where Were We", version=__version__, lifespan=lifespan)
    app.include_router(routes.router)
    app.include_router(webhooks.router)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def page(name: str, **ctx):
        def handler(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                name,
                {"version": __version__, "path": request.url.path, **ctx, **request.path_params},
            )

        return handler

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
    app.add_api_route("/indexing", page("indexing.html"), methods=["GET"], include_in_schema=False)
    app.add_api_route("/settings", page("settings.html"), methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/favicon.ico",
        lambda: RedirectResponse("/static/icon.svg"),
        methods=["GET"],
        include_in_schema=False,
    )
    return app


app = create_app()
