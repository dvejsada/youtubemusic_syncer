"""Settings page components (Lidarr, Jellyfin, syncing defaults, YouTube Music login)."""
from __future__ import annotations

from fasthtml.common import (Code, Details, Div, Form, Input, Li, Ol, Option, Select, Small, Span, Strong, Summary,
                             Textarea)

from app.ui.core import badge, btn, field, icon, interval_select, page_header, panel, rel_text, section


def secret_input(name: str, saved: bool, id_: str):
    return Input(type="password", id=id_, name=name, value="", autocomplete="new-password", spellcheck="false",
                 placeholder="Saved. Leave blank to keep" if saved else "Paste API key")


def profile_fields(s: dict, options: dict | None = None):
    """Root folder / quality / metadata profile: selects when Lidarr answered, plain inputs otherwise."""
    def one(key, label, fid, hint=None):
        cur = s.get(key, "")
        names = (options or {}).get(key.removeprefix("lidarr_"), [])
        if names:
            vals = list(names) + ([cur] if cur and cur not in names else [])
            ctl = Select(Option("Default (first in Lidarr)", value="", selected=(cur == "")),
                         *[Option(v, value=v, selected=(v == cur)) for v in vals], id=fid, name=key)
        else:
            ctl = Input(type="text", id=fid, name=key, value=cur, placeholder="Default (first in Lidarr)")
        return field(label, ctl, hint, fr=fid)
    return Div(one("lidarr_root", "Root folder", "f-root", "Must be the folder your Jellyfin music library points to."),
               one("lidarr_quality", "Quality profile", "f-quality"),
               one("lidarr_metadata", "Metadata profile", "f-meta",
                   "Singles and EPs must be allowed in this profile, or single-only tracks can't be added."),
               id="lidarr-profiles", cls="fields cols-3")


def test_result(ok: bool, msg: str):
    return Small(icon("check" if ok else "alert"), " ", msg, cls="test-res " + ("ok" if ok else "err"), role="status")


def test_footer(service: str):
    """Card footer: a small ghost "Test connection" button next to its result text."""
    return panel(btn("Test connection", "refresh", "ghost", small=True, hx_post=f"/settings/test/{service}",
                     hx_include="#settings-form", hx_target=f"#{service}-test", hx_swap="innerHTML"),
                 Span(id=f"{service}-test"), foot=True)


def lidarr_section(s: dict):
    return section(
        "Lidarr", "Used to look up and download albums.",
        panel(Div(field("URL", Input(type="url", id="f-lurl", name="lidarr_url", value=s["lidarr_url"],
                                     placeholder="http://192.168.1.10:8686"), fr="f-lurl"),
                  field("API key", secret_input("lidarr_api_key", bool(s["lidarr_api_key"]), "f-lkey"),
                        "Lidarr, Settings, General, Security.", fr="f-lkey"), cls="fields"),
              # swapped for selects once Lidarr has answered with its folders and profiles
              Div(profile_fields(s), hx_get="/ui/lidarr-options", hx_trigger="load", hx_swap="innerHTML")),
        test_footer("lidarr"))


def jellyfin_section(s: dict):
    return section(
        "Jellyfin", "Used to build the playlist from tracks that are in your library.",
        panel(Div(field("URL", Input(type="url", id="f-jurl", name="jellyfin_url", value=s["jellyfin_url"],
                                     placeholder="http://192.168.1.10:8096"), fr="f-jurl"),
                  field("API key", secret_input("jellyfin_api_key", bool(s["jellyfin_api_key"]), "f-jkey"),
                        "Jellyfin, Dashboard, API Keys.", fr="f-jkey"), cls="fields"),
              field("User", Input(type="text", id="f-juser", name="jellyfin_user", value=s["jellyfin_user"],
                                  placeholder="First user"),
                    "The user whose library is searched and who owns the playlist. Empty = first user.",
                    fr="f-juser")),
        test_footer("jellyfin"))


def syncing_section(s: dict):
    return section(
        "Syncing", "How often new playlists refresh and how long to wait for Lidarr.",
        panel(Div(field("Default interval for new playlists",
                        interval_select(int(s["default_interval_hours"] or 24), "default_interval_hours", id="f-int"),
                        fr="f-int"),
                  field("Lidarr import wait (minutes)",
                        Input(type="number", id="f-wait", name="finalize_timeout_min", min="1", max="240",
                              value=s["finalize_timeout_min"]),
                        "How long to wait for Lidarr to import new artists before monitoring their albums anyway. "
                        "Syncs don't wait for it; this is checked in the background.", fr="f-wait"),
                  cls="fields")))


def ytm_steps():
    return Ol(Li("Open ", Strong("music.youtube.com"), " in your browser while signed in."),
              Li("Open the developer tools (F12), go to the ", Strong("Network"), " tab and filter by ",
                 Code("/browse"), "."),
              Li("Click a request, find ", Strong("Request Headers"), " and copy everything from ",
                 Code("accept"), " down."),
              Li("Paste it above. The headers are stored on this server only (", Code("browser.json"),
                 " in the data folder)."))


def ytm_section(configured: bool):
    return section(
        "YouTube Music login",
        "Needed only for private playlists and Liked Music. Public, unlisted and mix links work without it.",
        panel(Div(badge("ok", "Login configured", "check_circle"),
                  Form(btn("Remove login", "trash", "ghost", type="submit", small=True),
                       action="/settings/ytm/remove", method="post"), cls="row spread")) if configured else None,
        Form(panel(field("Replace with new headers" if configured else "Request headers",
                         Textarea(id="ytm-headers", name="headers", rows="6", spellcheck="false",
                                  placeholder="accept: */*\ncookie: …\nauthorization: SAPISIDHASH …\n"
                                              "x-goog-authuser: 0\n…"),
                         fr="ytm-headers"),
                   Details(Summary("How do I get these?"), ytm_steps())),
             panel(btn("Save login", "key", "outline", small=True, type="submit"), foot=True),
             action="/settings/ytm", method="post"),
        id="ytm")


def webhook_section(url: str, last: dict | None):
    """Lidarr webhook: off (a button to turn it on) or the URL to paste into Lidarr and the last event."""
    desc = ("Lidarr tells Playlist Sync when it has imported an album, so the Jellyfin playlist gets the new "
            "tracks a couple of minutes later instead of at the next scheduled sync.")
    if not url:
        return section("Lidarr webhook", desc,
                       panel(Div("Off.", cls="muted")),
                       panel(Form(btn("Turn on", "plus", "outline", small=True, type="submit"),
                                  action="/settings/webhook", method="post"), foot=True),
                       id="webhook")
    if last and last.get("event"):
        seen = f"Last event: {last['event']} {rel_text(last.get('at'))}"
        if last.get("album"):
            seen += f" ({last['album']}, {last.get('playlists', 0)} playlist(s) to update)"
    else:
        seen = "No event received yet: use Test in Lidarr."
    return section(
        "Lidarr webhook", desc,
        panel(field("Webhook URL", Input(type="text", id="f-hook", value=url, readonly=True, spellcheck="false"),
                    "In Lidarr: Settings, Connect, +, Webhook. Tick On Release Import, method POST, paste this "
                    "URL (use an address Lidarr can reach). Keep it secret: the token in it is the password.",
                    fr="f-hook"),
              Small(seen, cls="muted")),
        panel(Div(Form(btn("New URL", "refresh", "ghost", small=True, type="submit"),
                       action="/settings/webhook", method="post"),
                  Form(btn("Turn off", "trash", "ghost", small=True, type="submit"),
                       action="/settings/webhook/remove", method="post"), cls="row"), foot=True),
        id="webhook")


def settings_page_body(s: dict, ytm_configured: bool, hook_url: str = "", hook_last: dict | None = None):
    return (page_header("Settings", "Connections, sync defaults and your YouTube Music login."),
            Div(Form(lidarr_section(s), jellyfin_section(s), syncing_section(s),
                     Div(Small("Applies to Lidarr, Jellyfin and syncing.", cls="muted"),
                         btn("Save settings", "check", type="submit"), cls="save-bar"),
                     action="/settings", method="post", id="settings-form"),
                webhook_section(hook_url, hook_last), ytm_section(ytm_configured), cls="sections"))
