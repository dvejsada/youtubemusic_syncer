"""Shared UI building blocks used by every page: app shell, buttons, badges, artwork, form fields, time formatting.

Styling is Pico CSS (semantic HTML, a few classes); app.css adds the app-shell, the artwork-led components and
the dark-first theme.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fasthtml.common import (A, Aside, Button, Div, Form, H1, H2, Img, Input, Label, Main, NotStr, Option, P,
                             Section, Select, Small, Span, Strong, Title)

from app.db import parse_iso

APP_NAME = "Playlist Sync"

# ------------------------------------------------------------------- icons

_ICONS = {
    "refresh": '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M20.5 4v4.8h-4.8"/>',
    "pause": '<rect x="6.5" y="5" width="3.6" height="14" rx="1"/><rect x="13.9" y="5" width="3.6" height="14" rx="1"/>',
    "play": '<path d="M8 5.2v13.6a.6.6 0 0 0 .92.5l10.6-6.8a.6.6 0 0 0 0-1L8.92 4.7A.6.6 0 0 0 8 5.2z"/>',
    "trash": '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12.2A2 2 0 0 0 9 21h6a2 2 0 0 0 2-1.8L18 7"/><path d="M9 7V4.5h6V7"/>',
    "settings": '<path d="M4 6h7.5M17.5 6H20M4 12h2.5M12.5 12H20M4 18h10M19.5 18H20"/><circle cx="14.5" cy="6" r="2.5"/><circle cx="9.5" cy="12" r="2.5"/><circle cx="17" cy="18" r="2.5"/>',
    "logout": '<path d="M9 4H6.5A2.5 2.5 0 0 0 4 6.5v11A2.5 2.5 0 0 0 6.5 20H9"/><path d="M15.5 8L20 12l-4.5 4M20 12H9.5"/>',
    "external": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v3.5a2.5 2.5 0 0 1-2.5 2.5h-9A2.5 2.5 0 0 1 4 17.5v-9A2.5 2.5 0 0 1 6.5 6H10"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "check_circle": '<circle cx="12" cy="12" r="9"/><path d="M8 12.3l2.8 2.8L16 9.6"/>',
    "x_circle": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "warning": '<path d="M12 4l9 15.5H3z"/><path d="M12 10v4M12 17v.01"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "download": '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14"/>',
    "minus": '<path d="M6 12h12"/>',
    "chevron": '<path d="M9.5 6l6 6-6 6"/>',
    "back": '<path d="M19 12H5.5M11.5 6l-6 6 6 6"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/>',
    "music": '<path d="M9 17.5V5.5l11-2v12"/><circle cx="6.5" cy="17.5" r="2.5"/><circle cx="17.5" cy="15.5" r="2.5"/>',
    "library": '<rect x="4" y="4" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.6"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.6"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l8.5-8.5M16 7l2.5 2.5"/>',
}


def icon(name: str):
    return NotStr(f'<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '
                  f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_ICONS[name]}</svg>')


# ------------------------------------------------------------- htmx helpers

def busy(selector: str = "this") -> dict:
    """Pico spinner (aria-busy) on `selector` while the element's htmx request is in flight
    (the same idea as fasthtml.pico.PicoBusy, but able to point at a form's submit button)."""
    el = "this" if selector == "this" else f"this.querySelector('{selector}')"
    return {"hx_on__before_request": f"{el}?.setAttribute('aria-busy','true')",
            "hx_on__after_request": f"{el}?.removeAttribute('aria-busy')",
            "hx_disabled_elt": "this" if selector == "this" else f"find {selector}"}


def poll(url: str, every: str = "3s", swap: str = "outerHTML") -> dict:
    """Attributes that re-fetch the element periodically (the server answers 286 to stop it)."""
    return {"hx_get": url, "hx_trigger": f"every {every}", "hx_swap": swap}


# ----------------------------------------------------------------- buttons

# variant -> Pico button classes
_VARIANTS = {"primary": "", "secondary": "secondary", "outline": "outline secondary",
             "ghost": "outline secondary ghost", "danger": "danger"}


def btn(label: str | None, ico: str | None = None, variant: str = "primary", *, title: str | None = None,
        small: bool = False, **attrs):
    """A button with an optional icon. Icon-only when label is None. htmx buttons show a spinner while busy."""
    cls = " ".join(c for c in (_VARIANTS[variant], "icon-btn" if label is None else "", "sm" if small else "",
                               attrs.pop("cls", "")) if c)
    attrs.setdefault("type", "button")
    if label is None:
        attrs.setdefault("aria_label", title or "")
    if any(k in attrs for k in ("hx_post", "hx_get")):
        attrs = {**busy(), **attrs}
    return Button(icon(ico) if ico else None, Span(label) if label else None, cls=cls or None, title=title, **attrs)


def link_btn(label, href: str, variant: str = "outline", **attrs):
    """A link styled as a button (Pico: role=button)."""
    return A(label, href=href, role="button", cls=_VARIANTS[variant] or None, **attrs)


# ------------------------------------------------------------ small parts

def badge(tone: str, text, ico: str | None = None, *, busy: bool = False, title: str | None = None,
          compact: bool = False, **attrs):
    """Status pill: an icon (or a dot) and a label. tone: ok | warn | err | run | info | neutral.
    busy swaps the icon for a spinner. compact pills drop the label on narrow screens (the title keeps it)."""
    return Span(icon(ico) if ico else None, Span(text, cls="lbl"), cls=f"badge {tone}{' compact' if compact else ''}",
                aria_busy="true" if busy else None, title=title, **attrs)


def dot(tone: str, title: str | None = None):
    return Span(cls=f"dot {tone}", title=title)


def tag(text: str, title: str | None = None):
    return Small(text, cls="tag", title=title)


MOSAIC_TILES = 4


def _art_img(url: str):
    # sits on top of the gradient tile; a missing image renders as nothing (see .cover img in app.css)
    return Img(src=url, loading="lazy", alt="")


def cover(pl: dict, size: str = "md"):
    """Artwork tile for a playlist: always a gradient (hue derived from the id) with the cover image on top when
    there is one. Mixes have no cover: size != "xs" shows a 2x2 mosaic of pl["mosaic"] track ids instead.
    size: xs (sidebar) | md (lists) | fill (grid card, square) | xl (hero)."""
    pid, thumbs = pl["id"], pl.get("mosaic") or []
    if pl.get("cover_url"):
        art = _art_img(f"/art/playlist/{pid}")
    elif size == "xs" or not thumbs:
        art = None
    elif len(thumbs) < MOSAIC_TILES:
        art = _art_img(f"/art/track/{thumbs[0]}")
    else:
        art = Span(*[_art_img(f"/art/track/{t}") for t in thumbs[:MOSAIC_TILES]], cls="mosaic")
    return Span(icon("music"), art, cls=f"cover {size}", style=f"--h:{pid * 137 % 360}", aria_hidden="true")


def track_art(t: dict):
    """Album art thumbnail of a track row (gradient tile underneath, like cover())."""
    return Span(icon("music"), _art_img(f"/art/track/{t['id']}") if t.get("thumb_url") else None,
                cls="cover trk-art", style=f"--h:{(t.get('id') or 0) * 53 % 360}", aria_hidden="true")


def notice(tone: str, *content):
    """Inline message box. tone: err | warn | info."""
    return Div(icon("alert" if tone == "err" else "info"), Div(*content), cls=f"notice {tone}",
               role="alert" if tone == "err" else None)


def page_header(title: str, subtitle: str | None = None, actions=None):
    """Page title row shared by Library and Settings: h1, an optional muted subtitle, optional actions on the right."""
    return Div(Div(H1(title), P(subtitle, cls="muted") if subtitle else None, cls="page-title"),
               Div(actions, cls="page-actions") if actions is not None else None, cls="page-head")


def section(title: str, desc: str | None, *blocks, el=Section, cls: str = "", **attrs):
    """Settings layout: title and description on the left, a card of `panel` blocks on the right."""
    return el(Div(H2(title), P(desc) if desc else None, cls="set-info"), Div(*blocks, cls="set-card"),
              cls=f"set-sec {cls}".strip(), **attrs)


def panel(*content, foot: bool = False):
    """A block inside a `section` card; foot=True is the hairline-separated footer row (actions, results)."""
    return Div(*content, cls="set-foot" if foot else "set-body")


def empty(title: str, text: str, ico: str = "music"):
    return Div(Div(icon(ico), cls="empty-art"), Strong(title), P(text, cls="muted"), cls="empty")


def field(label: str, control, hint: str | None = None, fr: str | None = None):
    return Div(Label(label, fr=fr), control, Small(hint) if hint else None)


def switch(name: str, label: str, desc: str, checked: bool):
    """Pico switch (a checkbox with role=switch) with a description line."""
    return Label(Input(type="checkbox", name=name, checked=checked, role="switch"), label,
                 Small(desc, cls="muted switch-desc"))


INTERVALS = [(6, "Every 6 hours"), (12, "Every 12 hours"), (24, "Daily"), (168, "Weekly")]


def interval_label(h: int) -> str:
    return dict(INTERVALS).get(h, f"Every {h} hours")


def interval_select(selected: int, name: str = "interval_hours", **attrs):
    opts = list(INTERVALS)
    if selected not in dict(opts):
        opts.append((selected, interval_label(selected)))
    return Select(*[Option(lbl, value=str(v), selected=(v == selected)) for v, lbl in opts], name=name, **attrs)


# -------------------------------------------------------------------- time

def local_str(d: datetime) -> str:
    return d.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _span_text(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 48 * 3600:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} d"


def rel_text(iso: str | None, now: datetime | None = None) -> str:
    d = parse_iso(iso)
    if d is None:
        return "never"
    now = now or datetime.now(timezone.utc)
    delta = (d - now).total_seconds()
    if abs(delta) < 45:
        return "just now" if delta <= 0 else "any moment"
    txt = _span_text(abs(delta))
    return f"in {txt}" if delta > 0 else f"{txt} ago"


def rel_time(iso: str | None):
    d = parse_iso(iso)
    if d is None:
        return Span("never", cls="muted")
    return Span(rel_text(iso), title=local_str(d))


def duration_text(start: str | None, end: str | None) -> str:
    a, b = parse_iso(start), parse_iso(end)
    if not a or not b:
        return "-"
    s = int((b - a).total_seconds())
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


# ------------------------------------------------------------------ layout

def brand():
    return A(Span(icon("music"), cls="brand-mark"), Strong(APP_NAME), href="/", cls="brand")


def conn_item(name: str, ok: bool, msg: str):
    if not ok and msg == "Not configured":
        return A(dot("neutral"), Span(name), Small("not set up"), href="/settings", cls="conn-item",
                 title=f"Configure {name} in Settings")
    return Span(dot("ok" if ok else "err"), Span(name), Small("connected" if ok else "error"), cls="conn-item",
                title=msg if ok else "Error: " + msg)


def conn_strip(results: dict | None = None):
    """Connection status of Lidarr / Jellyfin. Without results it renders placeholders that load themselves."""
    if results is None:
        return Div(*[Span(dot("neutral"), Span(n), cls="conn-item") for n in ("Lidarr", "Jellyfin")],
                   id="conn", hx_get="/ui/connections", hx_trigger="load", hx_swap="outerHTML")
    return Div(*[conn_item(n, *r) for n, r in results.items()], id="conn")


def sidebar(active: str, playlists=None):
    """Left sidebar on desktop, a compact sticky top bar on tablet and phone (CSS only).
    `playlists` is the ready-made playlist list for the sidebar (see ui.playlists.sidebar_playlists)."""
    def link(label, ico, href, key):
        return A(icon(ico), Span(label), href=href, cls="nav-link", aria_current="page" if active == key else None)
    return Aside(
        brand(),
        Div(link("Library", "library", "/", "library"), link("Settings", "settings", "/settings", "settings"),
            cls="side-nav", role="navigation", aria_label="Main"),
        Div(Small("Playlists", cls="side-title"), playlists, cls="side-pls") if playlists is not None else None,
        Div(conn_strip(),
            Form(btn("Log out", "logout", "ghost", small=True, type="submit"), action="/logout", method="post"),
            cls="side-foot"),
        cls="sidebar")


def page(title: str, *content, active: str = "", playlists=None):
    """A full signed-in page: <title>, the app shell (sidebar / top bar) and the main column."""
    return (Title(f"{title} · {APP_NAME}"),
            Div(sidebar(active, playlists), Main(*content), cls="shell"))
