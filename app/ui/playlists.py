"""Playlist components: library grid, sidebar list, add form and the playlist detail page (hero, tracks, runs)."""
from __future__ import annotations

from fasthtml.common import (A, Details, Div, Fieldset, Form, H1, Input, Pre, Progress, Section, Small, Span, Strong,
                             Summary)
from fasthtml.pico import Group

from app.ui.core import (badge, btn, busy, cover, dot, duration_text, empty, field, icon, interval_label,
                         interval_select, link_btn, notice, page_header, panel, poll, rel_text, rel_time, section, switch, tag,
                         track_art)

# ------------------------------------------------------------------- state

STATE_ICONS = {"syncing": "refresh", "queued": "clock", "paused": "pause", "ok": "check_circle",
               "warning": "warning", "error": "x_circle", "new": "info"}


def playlist_state(pl: dict, running_id: int | None, queue: list[int]) -> tuple[str, str, str]:
    """(key, label, badge tone) of a playlist."""
    if running_id == pl["id"]:
        return "syncing", "Syncing…", "run"
    if pl["id"] in queue or pl.get("queued"):
        return "queued", "Queued", "run"
    if not pl["enabled"]:
        return "paused", "Paused", "neutral"
    return {"ok": ("ok", "Synced", "ok"), "warning": ("warning", "Needs attention", "warn"),
            "error": ("error", "Error", "err")}.get(pl.get("last_status"), ("new", "Not synced yet", "neutral"))


def is_active(state) -> bool:
    return state[0] in ("syncing", "queued")


def state_badge(state, pl: dict):
    key, label, tone = state
    return badge(tone, label, STATE_ICONS[key], busy=key == "syncing",
                 title=(pl.get("last_error") if key in ("error", "warning") else None) or label)


def mix_tag():
    return tag("Mix", "Auto-generated mix: its content changes over time")


def tracks_text(n: int) -> str:
    return f"{n} track{'s' if n != 1 else ''}"


def name_of(pl: dict) -> str:
    return pl["name"] or pl["ref"]


# ----------------------------------------------------------------- library

def progress(label: str, value: int, total: int, enabled: bool = True):
    """Thin meter: label and count above a 3px bar."""
    if not enabled:
        return Div(Small(Span(label), Span("off")), Progress(value="0", max="1"), cls="prog off")
    return Div(Small(Span(label), Strong(f"{value}/{total}")), Progress(value=str(value), max=str(total or 1)),
               cls="prog done" if total and value >= total else "prog", title=f"{label}: {value} of {total} tracks")


def _card_meta(pl: dict, key: str, total: int) -> tuple[str, str | None]:
    """(meta line, tooltip) of a library card."""
    if key == "syncing":
        return "Sync in progress", None
    if key == "queued":
        return "Waiting for the worker", None
    if not pl["last_run_at"]:
        return f"{tracks_text(total)} · not synced yet", None
    next_run = f"Next sync {rel_text(pl['next_run_at'])}" if pl["enabled"] and pl["next_run_at"] else None
    return f"{tracks_text(total)} · synced {rel_text(pl['last_run_at'])}", next_run


def playlist_card(pl: dict, running_id: int | None, queue: list[int]):
    """A library grid card. The card is the htmx polling element while its playlist is syncing or queued; the name
    link is stretched over the whole card, the quick actions sit above it."""
    state = playlist_state(pl, running_id, queue)
    key, pid, total = state[0], pl["id"], pl["track_count"] or 0
    meta, meta_title = _card_meta(pl, key, total)
    target = dict(hx_target=f"#pl-{pid}", hx_swap="outerHTML")
    error = pl.get("last_error") if key in ("error", "warning") else None
    return Div(
        Div(cover(pl, "fill"), Span(state_badge(state, pl), cls="pc-state"),
            Div(btn(None, "refresh", "ghost", title="Sync now", cls="round", disabled=is_active(state),
                    hx_post=f"/playlists/{pid}/sync", **target),
                btn(None, "pause" if pl["enabled"] else "play", "ghost", cls="round",
                    title="Pause automatic sync" if pl["enabled"] else "Resume automatic sync",
                    hx_post=f"/playlists/{pid}/toggle", **target),
                cls="pc-actions"),
            cls="pc-art"),
        Div(Div(A(name_of(pl), href=f"/playlists/{pid}" + ("?tab=runs" if key == "syncing" else ""), cls="pc-name"),
                mix_tag() if pl["is_mix"] else None, cls="row"),
            Small(meta, cls="pc-meta muted", title=meta_title),
            Small(error, cls="pl-error" + (" warn" if key == "warning" else ""), title=error) if error else None,
            cls="pc-text"),
        Div(progress("Lidarr", pl["lidarr_found"], total, bool(pl["do_lidarr"])),
            progress("Jellyfin", pl["jellyfin_matched"], total - (pl["jellyfin_duplicates"] or 0),
                     bool(pl["do_jellyfin"])), cls="pc-meters"),
        id=f"pl-{pid}", cls="pcard", data_state=key,
        **(poll(f"/ui/playlists/{pid}/row") if is_active(state) else {}))


def library_summary(playlists: list[dict]) -> str:
    attention = sum(p.get("last_status") in ("warning", "error") for p in playlists)
    parts = [f"{len(playlists)} playlist{'s' if len(playlists) != 1 else ''}",
             tracks_text(sum(p["track_count"] or 0 for p in playlists))]
    if attention:
        parts.append(f"{attention} {'needs' if attention == 1 else 'need'} attention")
    return " · ".join(parts)


def playlist_list(playlists: list[dict], running_id: int | None, queue: list[int]):
    if playlists:
        body = (Div(library_summary(playlists), cls="lib-bar muted"),
                Div(*[playlist_card(p, running_id, queue) for p in playlists], cls="pgrid"))
    else:
        body = (empty("No playlists yet",
                      "Paste a YouTube Music playlist link above. Playlist Sync looks every track up, asks Lidarr to "
                      "download the albums and builds a matching playlist in Jellyfin, then repeats on a schedule."),)
    # re-rendered whenever a response sends the "playlists-changed" event (HX-Trigger)
    return Div(*body, id="playlists", hx_get="/ui/playlists", hx_trigger="playlists-changed from:body",
               hx_swap="outerHTML")


def add_form(default_interval: int, error: str | None = None, value: str = "", autofocus: bool = False):
    return Div(
        Form(
            field("Add a playlist", Group(
                Input(type="text", id="playlist-url", name="url", value=value, required=True,
                      placeholder="Paste a YouTube Music playlist link…", autocomplete="off",
                      spellcheck="false", autofocus=autofocus, aria_invalid="true" if error else None,
                      aria_describedby="add-help"),
                interval_select(default_interval, aria_label="Sync interval"),
                btn("Add playlist", "plus", type="submit")), fr="playlist-url"),
            Small(error, id="add-help", role="alert") if error else
            Small("Public, unlisted and auto-generated mix links work out of the box. Private playlists need a "
                  "YouTube Music login in Settings.", id="add-help"),
            hx_post="/playlists", hx_target="#add-card", hx_swap="outerHTML", **busy("[type=submit]")),
        id="add-card")


def dashboard(playlists, running_id, queue, default_interval):
    sync_all = btn("Sync all", "refresh", "outline", small=True, title="Queue every active playlist", cls="sync-all",
                   hx_post="/playlists/sync-all", hx_swap="none")
    return (page_header("Library", actions=sync_all),
            add_form(default_interval, autofocus=not playlists),
            playlist_list(playlists, running_id, queue))


def sidebar_playlists(playlists: list[dict], running_id: int | None, queue: list[int], current: int | None = None):
    """Sidebar playlist list (name and status dot); it refreshes itself on "playlists-changed"."""
    def item(pl):
        _, label, tone = playlist_state(pl, running_id, queue)
        return A(cover(pl, "xs"), Span(name_of(pl), cls="side-name"), dot(tone, label), href=f"/playlists/{pl['id']}",
                 cls="side-pl", aria_current="page" if pl["id"] == current else None)
    return Div(*[item(p) for p in playlists] or [Small("Nothing here yet", cls="muted side-empty")], id="side-pls",
               hx_get="/ui/sidebar-playlists" + (f"?current={current}" if current else ""),
               hx_trigger="playlists-changed from:body", hx_swap="outerHTML")


# ------------------------------------------------------------- detail page

# status -> (tone, label, icon)
LIDARR_BADGES = {
    "downloaded": ("ok", "Downloaded", "check_circle"), "added": ("ok", "Added", "check_circle"),
    "monitored": ("run", "Monitored", "clock"), "duplicate": ("ok", "Same release", "check_circle"),
    "not_found": ("warn", "Not found", "warning"), "error": ("err", "Error", "x_circle"),
    "skipped": ("neutral", "Skipped", "minus"), "pending": ("neutral", "Pending", "clock"),
}
JELLYFIN_BADGES = {
    "in_playlist": ("ok", "In playlist", "check_circle"), "added": ("ok", "Added", "check_circle"),
    "duplicate": ("neutral", "Same song", "minus"), "missing": ("warn", "Not in library", "warning"),
    "not_added": ("err", "Not added", "x_circle"), "skipped": ("neutral", "Skipped", "minus"),
    "pending": ("neutral", "Pending", "clock"),
}
LIDARR_MISSING = ("not_found", "error")


def figure(value, label: str, tone: str = ""):
    return Div(Strong(str(value)), Span(label), cls=f"fig {tone}".strip())


def hero_backdrop(pl: dict):
    """The artwork again, blurred and dimmed behind the hero (CSS does the blur). No art: the gradient shows."""
    return Div(cover(pl, "backdrop"), cls="hero-bg", aria_hidden="true")


def detail_head(pl: dict, stats: dict, state):
    pid, total = pl["id"], pl["track_count"] or 0
    songs = total - stats["jf_dup"]  # a track that repeats an earlier song is in the playlist once
    active = is_active(state)
    lid_on, jf_on = bool(pl["do_lidarr"]), bool(pl["do_jellyfin"])
    err = pl.get("last_error")
    own = dict(hx_target="#detail-head", hx_swap="outerHTML")
    return Section(
        hero_backdrop(pl),
        Div(A(icon("back"), Span("Library"), href="/", cls="hero-back"),
            Div(cover(pl, "xl"),
                Div(Small("Mix" if pl["is_mix"] else "Playlist", cls="eyebrow"),
                    H1(name_of(pl), cls="hero-title"),
                    Div(state_badge(state, pl), Span(interval_label(pl["interval_hours"])),
                        Span("synced ", rel_time(pl["last_run_at"])) if pl["last_run_at"] else Span("not synced yet"),
                        A(icon("external"), Span("Open in YouTube Music"), href=pl["url"] or "#", target="_blank",
                          rel="noopener noreferrer"), cls="hero-meta"),
                    Div(btn("Sync now", "refresh", disabled=active, hx_post=f"/playlists/{pid}/sync", **own,
                            title="Sync this playlist now", cls="hero-sync"),
                        btn("Pause" if pl["enabled"] else "Resume", "pause" if pl["enabled"] else "play", "outline",
                            hx_post=f"/playlists/{pid}/toggle", **own,
                            title="Pause automatic sync" if pl["enabled"] else "Resume automatic sync"),
                        cls="hero-actions"),
                    cls="hero-text"),
                cls="hero-main"),
            Div(figure(total, "tracks"),
                figure(f"{stats['lidarr_ok']}/{total}" if lid_on else "-", "in Lidarr" if lid_on else "Lidarr is off",
                       "ok" if lid_on and total and stats["lidarr_ok"] == total else ""),
                figure(f"{stats['jf_ok']}/{songs}" if jf_on else "-",
                       "in Jellyfin playlist" if jf_on else "Jellyfin is off",
                       "ok" if jf_on and songs and stats["jf_ok"] == songs else ""),
                figure(stats["jf_dup"], "repeat an earlier song") if jf_on and stats["jf_dup"] else None,
                figure(stats["lidarr_missing"], "not found", "warn") if lid_on and stats["lidarr_missing"] else None,
                figure(stats["jf_missing"], "not in Jellyfin library", "warn")
                if jf_on and stats["jf_missing"] else None,
                cls="figs"),
            notice("err" if state[0] == "error" else "warn", err) if err and state[0] in ("error", "warning") else None,
            cls="hero-body"),
        id="detail-head", cls="hero", style=f"--h:{pid * 137 % 360}",
        # while a sync is active the header polls; the server refreshes the page once it has finished
        **(poll(f"/ui/playlists/{pid}/head?live=1") if active else {}))


def tabs(pid: int, current: str, counts: dict):
    def tab(key, label):
        n = counts.get(key)
        return A(label, Small(str(n), cls="count") if n is not None else None, href=f"/playlists/{pid}?tab={key}",
                 aria_current="page" if current == key else None)
    return Div(tab("tracks", "Tracks"), tab("runs", "Runs"), tab("settings", "Settings"), cls="tabs",
               role="navigation", aria_label="Playlist sections")


def _status_cell(table: dict, status: str, release: str = "", note: str = "", noted=LIDARR_MISSING):
    tone, label, ico = table.get(status, ("neutral", status or "-", "minus"))
    detail = " · ".join(x for x in (release, note) if x)
    return Div(badge(tone, label, ico, compact=True, title=f"{label}: {detail}" if detail else label),
               Small(note, cls="trk-note", title=note) if note and status in noted else None,
               cls="trk-status")


def tracks_panel(pid: int, tracks: list[dict], flt: str):
    filters = {"all": ("All", lambda t: True),
               "lidarr": ("Not found in Lidarr", lambda t: t["lidarr_status"] in LIDARR_MISSING),
               "jellyfin": ("Not in Jellyfin yet", lambda t: t["jellyfin_status"] == "missing")}
    chips = Div(*[A(label, Small(str(sum(map(fn, tracks)))), href=f"/playlists/{pid}?tab=tracks&filter={key}",
                    cls="seg", aria_current="true" if flt == key else None)
                  for key, (label, fn) in filters.items()], cls="segs")
    if not tracks:
        return chips, Div("No tracks yet. They appear after the first sync has finished.", cls="center muted pad")
    shown = [t for t in tracks if filters[flt][1](t)]
    rows = [Div(Span(str(t["position"]), cls="trk-n"), track_art(t),
                Div(Strong(t["title"], cls="trk-title"), Small(", ".join(t["artists"]), cls="trk-artist"),
                    cls="trk-main"),
                Span(t["album"] or "", cls="trk-album"),
                _status_cell(LIDARR_BADGES, t["lidarr_status"], t["lidarr_release"], t["lidarr_note"]),
                _status_cell(JELLYFIN_BADGES, t["jellyfin_status"], note=t["jellyfin_note"], noted=("duplicate",)),
                cls="trk")
            for t in shown] or [Div("Nothing matches this filter.", cls="center muted pad")]
    head = Div(Span("#", cls="trk-n"), Span(), Span("Title"), Span("Album", cls="trk-album"), Span("Lidarr"),
               Span("Jellyfin"), cls="trk trk-head")
    return chips, Div(head, *rows, cls="tracklist")


TRIGGER_LABELS = {"manual": "Manual sync", "schedule": "Scheduled", "added": "First sync"}
# status -> (tone, label, icon)
RUN_BADGES = {"running": ("run", "Running", "refresh"), "ok": ("ok", "Success", "check_circle"),
              "warning": ("warn", "Warnings", "warning"), "error": ("err", "Failed", "x_circle"),
              "interrupted": ("neutral", "Interrupted", "minus")}
MAX_LOG_CHARS = 200_000


def log_text(run: dict) -> str:
    text = (run.get("log") or "").rstrip("\n")
    if len(text) > MAX_LOG_CHARS:
        text = "… (earlier output trimmed)\n" + text[-MAX_LOG_CHARS:]
    return text or ("Waiting for output…" if run["status"] == "running" else "No output.")


def log_view(run: dict):
    """The run log. A running log polls its text; the server answers 286 when the run is over, which
    stops htmx polling. The .log box keeps itself scrolled to the bottom (CSS column-reverse)."""
    live = run["status"] == "running"
    return Div(Pre(log_text(run), id=f"log-{run['id']}",
                   **(poll(f"/ui/runs/{run['id']}/log", "2s", "innerHTML") if live else {})),
               cls="log", tabindex="0")


def run_summary_text(run: dict) -> str:
    s = run.get("summary") or {}
    if "tracks" not in s:
        return (s.get("errors") or [""])[0] or ""
    parts = [f"{s['tracks']} tracks", f"Lidarr {s.get('lidarr_found', 0)}", f"Jellyfin {s.get('jellyfin_matched', 0)}"]
    if s.get("jellyfin_added"):
        parts.append(f"{s['jellyfin_added']} added to playlist")
    if s.get("jellyfin_duplicates"):
        parts.append(f"{s['jellyfin_duplicates']} repeat an earlier song")
    if s.get("lidarr_failed"):
        parts.append(f"{s['lidarr_failed']} not found")
    return " · ".join(parts)


def runs_panel(runs: list[dict]):
    if not runs:
        return Div("No runs yet.", cls="center muted pad")

    def run_item(r):
        live = r["status"] == "running"
        tone, label, ico = RUN_BADGES.get(r["status"], ("neutral", r["status"], "minus"))
        return Details(
            Summary(Span(icon(ico), cls=f"run-dot {tone}", title=label, aria_busy="true" if live else None),
                    Div(Strong(TRIGGER_LABELS.get(r["trigger"], r["trigger"] or "")), Span(" · "),
                        rel_time(r["started_at"]), cls="run-when"),
                    Small(run_summary_text(r), cls="run-sum muted"),
                    Small(duration_text(r["started_at"], r["finished_at"]) if r["finished_at"] else "running…",
                          cls="run-dur muted")),
            log_view(r), open=live, id=f"run-{r['id']}", data_status=r["status"])
    return Div(*map(run_item, runs), cls="timeline")


def playlist_settings_panel(pl: dict):
    pid, name = pl["id"], name_of(pl)
    return Div(
        Form(
            section("Jellyfin playlist", "How this playlist is named in Jellyfin and how often it is refreshed.",
                    panel(Div(field("Jellyfin playlist name",
                                    Input(type="text", id="jf-name", name="jellyfin_name",
                                          value=pl["jellyfin_name"] or "",
                                          placeholder=pl["name"] or "Same as on YouTube Music"),
                                    "Leave empty to use the YouTube Music name.", fr="jf-name"),
                              field("Sync interval", interval_select(pl["interval_hours"], id="interval"),
                                    fr="interval"), cls="fields"))),
            section("Automation", "Choose what happens on every sync.",
                    panel(Fieldset(switch("enabled", "Automatic sync", "Refresh this playlist on the schedule above.",
                                          bool(pl["enabled"])),
                                   switch("do_lidarr", "Download with Lidarr",
                                          "Look up albums and ask Lidarr to fetch them.", bool(pl["do_lidarr"])),
                                   switch("do_jellyfin", "Update Jellyfin playlist",
                                          "Add matched tracks to a Jellyfin playlist.", bool(pl["do_jellyfin"])))),
                    panel(btn("Save changes", "check", type="submit"), foot=True)),
            action=f"/playlists/{pid}/settings", method="post"),
        section("Danger zone", "Irreversible actions for this playlist.",
                panel(Div("Removes it from Playlist Sync together with its tracks and run history. The Jellyfin "
                          "playlist and anything already added to Lidarr stay untouched.", cls="muted"),
                      btn("Delete playlist", "trash", "danger", hx_post=f"/playlists/{pid}/delete", hx_swap="none",
                          hx_confirm=f"Delete '{name}' from Playlist Sync? Its tracks and run history are removed; "
                                     "the Jellyfin playlist and Lidarr albums are NOT deleted.")),
                cls="danger"),
        cls="sections")


def not_found():
    return empty("Playlist not found", "It may have been deleted.", "alert"), link_btn("Back to the library", "/")
