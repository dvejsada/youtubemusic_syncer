"""Webhooks from other services (no session login: they authenticate with a token).

POST /hooks/lidarr: Lidarr, Settings, Connect, Webhook ("On Release Import"). The token is the webhook's
password (HTTP basic auth; recommended, it stays out of access logs) or ?token=.... After an album import the
playlists waiting for it get a Jellyfin-only update a moment later (see worker.IMPORT_DELAY).
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets

from fasthtml.common import APIRouter, Redirect, add_toast
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from app import db, worker

ar = APIRouter()

TOKEN_KEY, LAST_KEY = db.WEBHOOK_TOKEN, db.WEBHOOK_LAST
MAX_SHOWN = 200  # characters of an event's text kept for the Settings page


def _given_token(req) -> str:
    token = req.query_params.get("token", "")
    auth = req.headers.get("authorization", "")
    if not token and auth.lower().startswith("basic "):
        try:
            token = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace").partition(":")[2]
        except (binascii.Error, ValueError):
            token = ""
    return token


def _field(obj, name: str):
    """A payload field, whatever the casing (Lidarr sends camelCase, older versions PascalCase)."""
    if not isinstance(obj, dict):
        return None
    return next((v for k, v in obj.items() if k.lower() == name.lower()), None)


@ar.post("/hooks/lidarr")
async def lidarr_hook(req):
    token = await run_in_threadpool(db.get_meta, TOKEN_KEY)
    if not token:
        return Response(status_code=404)  # webhook turned off
    if not hmac.compare_digest(_given_token(req).encode(), token.encode()):
        return Response("invalid token", status_code=401)
    try:
        payload = await req.json()
    except (ValueError, RecursionError):
        return Response("expected JSON", status_code=400)
    event = str(_field(payload, "eventType") or "")
    artist = str(_field(_field(payload, "artist"), "name") or "")
    album = str(_field(_field(payload, "album"), "title") or "")
    # the database work runs in a thread, like the other routes (it may wait for the sync worker's writes)
    queued = await run_in_threadpool(_handle_event, event, artist, album)
    return JSONResponse({"ok": True, "event": event, "playlists": queued})


def _handle_event(event: str, artist: str, album: str) -> list[int]:
    queued: list[int] = []
    if event == "Download":
        queued = db.playlists_waiting_for(artist, album)
        db.schedule_jellyfin_update(queued, worker.IMPORT_DELAY)
    db.set_meta(LAST_KEY, json.dumps({"at": db.now_iso(), "event": (event or "?")[:MAX_SHOWN],
                                      "album": f"{artist} - {album}"[:MAX_SHOWN] if artist and album else "",
                                      "playlists": len(queued)}, ensure_ascii=False))
    return queued


@ar.post("/settings/webhook")
def webhook_on(session):
    """Turn the webhook on, or give it a new password (the old one stops working)."""
    renew = bool(db.get_meta(TOKEN_KEY))
    db.set_meta(TOKEN_KEY, secrets.token_urlsafe(24))
    add_toast(session, "New webhook password created: update it in Lidarr" if renew else "Lidarr webhook turned on",
              "success")
    return Redirect("/settings#webhook")


@ar.post("/settings/webhook/remove")
def webhook_off(session):
    db.set_meta(TOKEN_KEY, "")
    db.set_meta(LAST_KEY, "")
    add_toast(session, "Lidarr webhook turned off", "success")
    return Redirect("/settings#webhook")
