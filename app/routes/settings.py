"""Settings page: Lidarr / Jellyfin connection, syncing defaults and the optional YouTube Music login."""
from __future__ import annotations

import json
import os

from fasthtml.common import APIRouter, Redirect, add_toast
from starlette.responses import Response

from app import db, engine
from app.config import MAX_FINALIZE_MIN, clamp, clamp_interval, clean_url
from app.routes.playlists import shell_page
from app.ui import settings as ui

ar = APIRouter()


@ar.get("/settings")
def settings_page(req):
    token = db.get_meta(db.WEBHOOK_TOKEN)
    hook = (f"{str(req.base_url).rstrip('/')}/hooks/lidarr", token) if token else None
    try:
        last = json.loads(db.get_meta(db.WEBHOOK_LAST) or "null")
    except ValueError:
        last = None
    return shell_page("Settings", *ui.settings_page_body(db.get_settings(), db.ytm_auth_file().exists(),
                                                         hook, last), active="settings")


def _key_overrides(lidarr_api_key: str, jellyfin_api_key: str) -> dict:
    """A blank API key input means 'keep the stored one'."""
    out = {}
    if lidarr_api_key.strip():
        out["lidarr_api_key"] = lidarr_api_key.strip()
    if jellyfin_api_key.strip():
        out["jellyfin_api_key"] = jellyfin_api_key.strip()
    return out


@ar.post("/settings")
def settings_save(session, lidarr_url: str = "", lidarr_api_key: str = "", lidarr_root: str = "",
                  lidarr_quality: str = "", lidarr_metadata: str = "", jellyfin_url: str = "",
                  jellyfin_api_key: str = "", jellyfin_user: str = "", default_interval_hours: int = 24,
                  finalize_timeout_min: int = 20):
    db.set_settings({
        "lidarr_url": clean_url(lidarr_url), "lidarr_root": lidarr_root, "lidarr_quality": lidarr_quality,
        "lidarr_metadata": lidarr_metadata, "jellyfin_url": clean_url(jellyfin_url), "jellyfin_user": jellyfin_user,
        "default_interval_hours": str(clamp_interval(default_interval_hours)),
        "finalize_timeout_min": str(clamp(finalize_timeout_min, 1, MAX_FINALIZE_MIN)),
        **_key_overrides(lidarr_api_key, jellyfin_api_key),
    })
    add_toast(session, "Settings saved", "success")
    return Redirect("/settings")


@ar.post("/settings/test/{service}")
def settings_test(service: str, lidarr_url: str = "", lidarr_api_key: str = "", jellyfin_url: str = "",
                  jellyfin_api_key: str = "", jellyfin_user: str = ""):
    """Test the values currently in the form (saved API keys are used for blank key inputs)."""
    check = {"lidarr": engine.check_lidarr, "jellyfin": engine.check_jellyfin}.get(service)
    if not check:
        return Response(status_code=404)
    st = db.engine_settings({"lidarr_url": clean_url(lidarr_url), "jellyfin_url": clean_url(jellyfin_url),
                             "jellyfin_user": jellyfin_user.strip(),
                             **_key_overrides(lidarr_api_key, jellyfin_api_key)})
    ok, msg = check(st)
    if not ok and msg == "Not configured":
        msg = "Enter a URL and an API key first."
    return ui.test_result(ok, msg)


@ar.get("/ui/lidarr-options")
def ui_lidarr_options():
    opts = engine.lidarr_options(db.engine_settings())
    if not any(opts.values()):
        return Response(status_code=204)  # htmx keeps the plain inputs
    return ui.profile_fields(db.get_settings(), opts)


@ar.post("/settings/ytm")
def ytm_save(session, headers: str = ""):
    def error(msg: str):
        add_toast(session, msg[:300], "error")
        return Redirect("/settings#ytm")

    headers = headers.strip()
    if not headers:
        return error("Paste the request headers first.")
    if "sapisidhash" not in headers.lower():
        return error("No 'authorization: SAPISIDHASH …' line found. Copy the request headers of an "
                     "authenticated request to music.youtube.com.")
    import ytmusicapi
    target = db.ytm_auth_file()
    tmp = target.with_suffix(".json.tmp")
    try:
        ytmusicapi.setup(filepath=str(tmp), headers_raw=headers)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return error(f"Could not use these headers: {type(e).__name__}: {str(e)[:200]}")
    add_toast(session, "YouTube Music login saved", "success")
    return Redirect("/settings")


@ar.post("/settings/ytm/remove")
def ytm_remove(session):
    db.ytm_auth_file().unlink(missing_ok=True)
    add_toast(session, "YouTube Music login removed", "success")
    return Redirect("/settings")
