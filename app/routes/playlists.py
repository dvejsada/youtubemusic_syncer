"""Dashboard, playlist detail page and the playlist actions (add, sync, pause/resume, settings, delete).

Fragments under /ui/... are what htmx polls or lazy-loads. Messages are FastHTML toasts (add_toast), which
also survive a redirect, so they double as flash messages.
"""
from __future__ import annotations

import html
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fasthtml.common import APIRouter, HtmxHeaders, HtmxResponseHeaders, Redirect, add_toast
from starlette.responses import Response

from app import db, engine, worker
from app.config import REF_RE, clamp_interval
from app.ui import playlists as ui
from app.ui.core import conn_strip, page

ar = APIRouter()

CHANGED = HtmxResponseHeaders(trigger="playlists-changed")  # makes the dashboard list re-render itself
STOP_POLLING = 286  # htmx stops an `every Ns` trigger when the response has this status


def _state():
    st = worker.status()
    return st["running_playlist_id"], st["queue"]


def _with_art(pl: dict) -> dict:
    """Mixes have no cover: attach the first track ids so the UI can build a mosaic from their thumbnails."""
    if pl["is_mix"] and not pl.get("cover_url"):
        pl["mosaic"] = db.mosaic_track_ids(pl["id"])
    return pl


def _playlists() -> list[dict]:
    return [_with_art(p) for p in db.list_playlists()]


def _playlist(pid: int) -> dict | None:
    pl = db.get_playlist(pid)
    return _with_art(pl) if pl else None


def shell_page(title: str, *content, active: str, current: int | None = None):
    """A signed-in page with the sidebar's playlist list filled in."""
    return page(title, *content, active=active,
                playlists=ui.sidebar_playlists(db.list_playlists(), *_state(), current=current))


def _default_interval() -> int:
    try:
        return clamp_interval(int(db.get_settings()["default_interval_hours"]))
    except ValueError:
        return 24


def _detail_head(pl: dict):
    running, queue = _state()
    return ui.detail_head(pl, db.track_stats(pl["id"]), ui.playlist_state(pl, running, queue))


def _fragment_for(pid: int, htmx: HtmxHeaders):
    """Actions are triggered from a dashboard row or from the detail header: answer with what was targeted."""
    pl = _playlist(pid)
    if htmx.target == "detail-head":
        return _detail_head(pl)
    return ui.playlist_card(pl, *_state())


# --------------------------------------------------------------- dashboard

@ar.get("/")
def index():
    running, queue = _state()
    return shell_page("Library", *ui.dashboard(_playlists(), running, queue, _default_interval()), active="library")


@ar.get("/ui/connections")
def ui_connections():
    st = db.engine_settings()
    with ThreadPoolExecutor(2) as ex:
        lidarr, jellyfin = ex.submit(engine.check_lidarr, st), ex.submit(engine.check_jellyfin, st)
        return conn_strip({"Lidarr": lidarr.result(), "Jellyfin": jellyfin.result()})


@ar.get("/ui/playlists")
def ui_playlists():
    return ui.playlist_list(_playlists(), *_state())


@ar.get("/ui/sidebar-playlists")
def ui_sidebar_playlists(current: int = 0):
    return ui.sidebar_playlists(db.list_playlists(), *_state(), current=current or None)


@ar.get("/ui/playlists/{pid}/row")
def ui_row(pid: int):
    pl = _playlist(pid)
    if not pl:
        return ""  # card vanished (deleted): htmx swaps it out
    return ui.playlist_card(pl, *_state())


@ar.post("/playlists")
def add_playlist(session, url: str = "", interval_hours: int = 0):
    default = _default_interval()
    interval = clamp_interval(interval_hours) if interval_hours > 0 else default
    url = url.strip()

    def fail(msg):
        return ui.add_form(interval, error=msg, value=url, autofocus=True)

    try:
        ref = engine.parse_playlist_ref(url)
    except engine.EngineError as e:
        return fail(str(e))
    if not REF_RE.match(ref):
        return fail("That does not look like a YouTube Music playlist link or ID.")
    if db.get_playlist_by_ref(ref):
        return fail("This playlist is already in your list.")
    ok, name_or_err, count = engine.check_ytm(db.engine_settings(), ref)
    if not ok:
        return fail(name_or_err)
    try:
        db.add_playlist(ref, f"https://music.youtube.com/playlist?list={ref}", name_or_err, count, interval,
                        is_mix=ref.startswith("RD"), cover_url=engine.take_cover(ref))
    except sqlite3.IntegrityError:
        return fail("This playlist is already in your list.")
    worker.wake()
    add_toast(session, f"Added '{name_or_err}' · {count} tracks · first sync started", "success")
    return ui.add_form(default), CHANGED


@ar.post("/playlists/sync-all")
def sync_all(session):
    running, _ = _state()
    n = 0
    for pl in db.list_playlists():
        if pl["enabled"] and pl["id"] != running and not pl["queued"]:
            db.queue_playlist(pl["id"])
            n += 1
    worker.wake()
    if n:
        add_toast(session, f"Queued {n} playlist{'s' if n != 1 else ''}", "success")
    else:
        add_toast(session, "Nothing to queue", "info")
    return CHANGED, ""


@ar.post("/playlists/{pid}/sync")
def sync_now(session, pid: int, htmx: HtmxHeaders):
    pl = db.get_playlist(pid)
    if not pl:
        add_toast(session, "Playlist not found", "error")
        return ()
    running, _ = _state()
    if running == pid or pl["queued"]:
        add_toast(session, "Already syncing or queued", "info")
    else:
        db.queue_playlist(pid)
        worker.wake()
        add_toast(session, f"Sync queued for '{pl['name']}'", "success")
    return _fragment_for(pid, htmx)


@ar.post("/playlists/{pid}/toggle")
def toggle(session, pid: int, htmx: HtmxHeaders):
    pl = db.get_playlist(pid)
    if not pl:
        add_toast(session, "Playlist not found", "error")
        return ()
    enabled = 0 if pl["enabled"] else 1
    fields = {"enabled": enabled}
    if enabled and not pl["next_run_at"]:
        fields["next_run_at"] = db.now_iso()
    db.update_playlist(pid, **fields)
    if enabled:
        worker.wake()
    add_toast(session, f"Automatic sync {'resumed' if enabled else 'paused'} for '{pl['name']}'", "success")
    return _fragment_for(pid, htmx)


@ar.post("/playlists/{pid}/delete")
def delete_playlist(session, pid: int):
    pl = db.get_playlist(pid)
    if not pl:
        return Redirect("/")
    if _state()[0] == pid:
        add_toast(session, "A sync is running for this playlist. Try again when it has finished.", "error")
        return ()
    db.delete_playlist(pid)
    add_toast(session, f"Deleted '{pl['name']}'", "success")
    return Redirect("/")


# ------------------------------------------------------------------ detail

@ar.get("/playlists/{pid}")
def detail(pid: int, tab: str = "tracks", filter: str = "all"):
    pl = _playlist(pid)
    if not pl:
        return shell_page("Not found", *ui.not_found(), active="library")
    tab = tab if tab in ("tracks", "runs", "settings") else "tracks"
    runs = db.list_runs(pid)
    if tab == "tracks":
        panel = ui.tracks_panel(pid, db.list_tracks(pid), filter if filter in ("lidarr", "jellyfin") else "all")
    elif tab == "runs":
        panel = ui.runs_panel(runs)
    else:
        panel = ui.playlist_settings_panel(pl)
    return shell_page(ui.name_of(pl),
                      _detail_head(pl),
                      ui.tabs(pid, tab, {"tracks": pl["track_count"] or 0, "runs": len(runs)}),
                      panel,
                      active="library", current=pid)


@ar.get("/ui/playlists/{pid}/head")
def ui_head(pid: int, live: int = 0):
    pl = _playlist(pid)
    if not pl:
        return HtmxResponseHeaders(redirect="/")
    head = _detail_head(pl)
    running, queue = _state()
    if live and not ui.is_active(ui.playlist_state(pl, running, queue)):
        # the sync we were watching has finished: reload so the tracks and runs are fresh
        return head, HtmxResponseHeaders(refresh="true")
    return head


@ar.get("/ui/runs/{rid}/log")
def ui_log(rid: int):
    """Text of a run log for the polling <pre>; 286 once the run has finished stops the polling."""
    run = db.get_run(rid)
    if not run:
        return Response(status_code=404)
    return Response(html.escape(ui.log_text(run)), media_type="text/html",
                    status_code=200 if run["status"] == "running" else STOP_POLLING)


@ar.post("/playlists/{pid}/settings")
def save_playlist_settings(session, pid: int, jellyfin_name: str = "", interval_hours: int = 24,
                           enabled: bool = False, do_lidarr: bool = False, do_jellyfin: bool = False,
                           jellyfin_mirror: bool = False):
    pl = db.get_playlist(pid)
    if not pl:
        return Redirect("/")
    interval_hours = clamp_interval(interval_hours)
    fields = dict(jellyfin_name=jellyfin_name.strip() or None, interval_hours=interval_hours,
                  enabled=int(enabled), do_lidarr=int(do_lidarr), do_jellyfin=int(do_jellyfin),
                  jellyfin_mirror=int(jellyfin_mirror))
    last = db.parse_iso(pl["last_run_at"])
    if enabled and last:
        fields["next_run_at"] = db.iso_in_hours(interval_hours, last)
    elif enabled and not pl["next_run_at"]:
        fields["next_run_at"] = db.now_iso()
    db.update_playlist(pid, **fields)
    if enabled:
        worker.wake()
    add_toast(session, "Playlist settings saved", "success")
    return Redirect(f"/playlists/{pid}?tab=settings")
