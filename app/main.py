"""FastHTML app setup. Run with: uvicorn app.main:app --host 0.0.0.0 --port 5001 (single worker).

Routes live in app/routes (one APIRouter per area), components in app/ui. Everything is served locally:
htmx and Pico CSS are vendored in app/static, no CDN is used.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fasthtml.common import Beforeware, Link, Meta, Redirect, Script, fast_app, setup_toasts
from starlette.responses import FileResponse, PlainTextResponse

from app import security
from starlette.staticfiles import StaticFiles

from app import db, worker
from app.config import SESSION_DAYS
from app.routes import routers
from app.ui.core import APP_NAME

log = logging.getLogger("playlist-sync")

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
TOAST_MS = 4500


def _secret_key() -> str:
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    f = db.data_dir() / ".secret_key"
    try:
        return f.read_text().strip() or _write_key(f)
    except FileNotFoundError:
        return _write_key(f)


def _write_key(f: Path) -> str:
    key = secrets.token_hex(32)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


def _static(name: str) -> str:
    """URL of a static file with a cache-busting version."""
    return f"/static/{name}?v={int((STATIC / name).stat().st_mtime)}"


def csrf_before(req):
    if security.is_cross_site(req):
        return PlainTextResponse("Cross-site request blocked", status_code=403)


def auth_before(req, session):
    if not session.get("auth") and not security.proxy_user(req):
        return Redirect("/login")


async def lifespan(app):  # must be a plain async generator (see FastHTML notes)
    db.init_db()
    worker.start()
    try:
        yield
    finally:
        worker.stop()


db.check_data_dir()  # readable error instead of a bare PermissionError from _secret_key()

app, rt = fast_app(
    # FastHTML's default headers load htmx & co. from CDNs: use the vendored copies instead
    default_hdrs=False, canonical=False,
    hdrs=(Meta(charset="utf-8"),
          Meta(name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
          Meta(name="color-scheme", content="light dark"),
          Link(rel="icon", href="/static/favicon.svg", type="image/svg+xml"),
          Link(rel="stylesheet", href=_static("pico.indigo.min.css")),
          Link(rel="stylesheet", href=_static("app.css")),
          Script(src="/static/htmx.min.js")),
    htmlkw=dict(lang="en"),
    # /hooks/... are called by other servers and authenticate with their own token (see routes/hooks.py)
    before=[Beforeware(csrf_before, skip=[r"/hooks/.*"]),
            Beforeware(auth_before, skip=[r"/login", r"/static/.*", r"/health", r"/favicon\.ico", r"/hooks/.*"])],
    secret_key=_secret_key(),
    session_cookie="ps_session",
    max_age=SESSION_DAYS * 24 * 3600,
    same_site="lax",
    title=APP_NAME,
    static_path=str(STATIC),
    lifespan=lifespan,
)
setup_toasts(app, duration=TOAST_MS)

# fast_app's catch-all static route serves any file by extension without a containment check: use a
# StaticFiles mount (which rejects path traversal) instead.
app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/{fname:path}.{ext:static}"]
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

for router in routers:
    router.to_app(app)


@rt("/health", methods=["GET"])
def health():
    # a dead sync thread means nothing syncs any more: report it so Docker restarts the container
    if not worker.is_alive():
        return PlainTextResponse("sync worker is not running", status_code=503)
    return PlainTextResponse("ok")


@rt("/favicon.ico", methods=["GET"])
def favicon():
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")
