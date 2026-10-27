"""Tautulli webhook receiver."""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import load_settings
from ..services.playback import handle_webhook
from .deps import get_session

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhooks/tautulli")
async def tautulli_webhook(request: Request, session: Session = Depends(get_session)) -> dict:
    settings = load_settings(session)
    if settings.webhook_secret:
        supplied = request.query_params.get("token") or request.headers.get("x-webhook-secret", "")
        if not hmac.compare_digest(supplied, settings.webhook_secret):
            raise HTTPException(401, "bad webhook token")
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        form = await request.form()
        payload = dict(form)
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a JSON object")
    result = handle_webhook(session, payload, settings)
    session.commit()
    log.info("tautulli webhook %s: %s", payload.get("action"), result.get("result"))
    return result
