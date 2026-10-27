"""Sign in / sign out (one shared password from APP_PASSWORD)."""
from __future__ import annotations

import asyncio
import hmac
import os
import time

from fasthtml.common import APIRouter, FtResponse, Redirect

from app import security
from app.ui.auth import login_page

ar = APIRouter()

LOGIN_DELAY = 1.0       # seconds of delay after a wrong password (tests set this to 0)
LOGIN_MAX_QUEUE = 30.0  # seconds of queued failed attempts after which we answer 429 right away
_next_fail_slot = 0.0


def _password() -> str:
    return os.environ.get("APP_PASSWORD", "")


@ar.get("/login")
def login_form(req, session):
    if session.get("auth") or security.proxy_user(req):
        return Redirect("/")
    return login_page(not_configured=not _password(), proxy_only=not _password() and security.proxy_auth_configured())


@ar.post("/login")
async def login_submit(session, password: str = ""):
    expected = _password()
    if not expected:
        return FtResponse(login_page(not_configured=True, proxy_only=security.proxy_auth_configured()),
                          status_code=503)
    if hmac.compare_digest(password.encode(), expected.encode()):
        session.clear()
        session["auth"] = True
        return Redirect("/")
    # Failed attempts are served one per LOGIN_DELAY, globally, without blocking a worker thread
    # (a time.sleep here let a burst of guesses exhaust the thread pool, /health included).
    global _next_fail_slot
    now = time.monotonic()
    slot = max(now, _next_fail_slot) + LOGIN_DELAY
    if slot - now > LOGIN_MAX_QUEUE:
        return FtResponse(login_page(error="Too many attempts. Try again in a minute."), status_code=429)
    _next_fail_slot = slot
    await asyncio.sleep(slot - now)
    return FtResponse(login_page(error="Incorrect password."), status_code=401)


@ar.post("/logout")
def logout(session):
    session.clear()
    return Redirect("/login")
