"""YouTube Music: reading playlists (normal ones and auto-generated mixes) and the song version of videos."""
from __future__ import annotations

import re
from dataclasses import replace

from ytmusicapi import YTMusic

from app.engine.cache import Cache
from app.engine.common import _redactor
from app.engine.matching import clean, sim, split_title
from app.engine.models import EngineError, Settings


def parse_playlist_ref(url_or_id: str) -> str:
    """Extract the playlist id (the ``list=`` parameter of a URL, or the bare id)."""
    arg = (url_or_id or "").strip()
    m = re.search(r"[?&]list=([\w-]+)", arg)
    ref = m.group(1) if m else arg
    if not ref:
        raise EngineError("No playlist URL or ID given.")
    return ref


def _make_ytm(settings: Settings):
    """Factory for the YouTube Music client (tests replace this with a stub)."""
    return YTMusic(settings.ytm_auth_path) if settings.ytm_auth_path else YTMusic()


def _thumb_width(th: dict) -> int:
    try:
        return int(th.get("width") or 0)
    except (TypeError, ValueError):
        return 0


def _usable_thumbs(thumbs) -> list[dict]:
    """The usable entries of a ytmusicapi thumbnail list (missing / None / malformed -> [])."""
    if not isinstance(thumbs, list):
        return []
    return [t for t in thumbs if isinstance(t, dict) and isinstance(t.get("url"), str) and t["url"]]


def pick_cover(thumbs) -> str:
    """Largest thumbnail of a playlist ('' if there is none)."""
    best = max(_usable_thumbs(thumbs), key=_thumb_width, default=None)
    return best["url"] if best else ""


def pick_thumb(thumbs, min_width: int = 60) -> str:
    """Smallest track thumbnail that is at least `min_width` wide (else the largest one; '' if none)."""
    items = _usable_thumbs(thumbs)
    big_enough = [t for t in items if _thumb_width(t) >= min_width]
    best = min(big_enough, key=_thumb_width) if big_enough else max(items, key=_thumb_width, default=None)
    return best["url"] if best else ""


def fetch_raw(ytm, pid: str, log=None):
    """Returns (title, raw_tracks, is_mix, cover_url). Handles normal playlists and auto-generated
    mixes (RD...); mixes have no cover (cover_url "")."""
    log = log or (lambda m="": None)
    pl, err = None, None
    try:
        pl = ytm.get_playlist(pid, limit=None)
    except Exception as e:
        err = e
    if pl and pl.get("tracks"):
        return pl.get("title") or pid, pl["tracks"], False, pick_cover(pl.get("thumbnails"))
    if pid.startswith("RD"):
        # Mixes / radios ("My Supermix", "Discover Mix", ...) only open as a watch queue.
        # They're personalised and change over time - every run may return different songs.
        wp = ytm.get_watch_playlist(playlistId=pid, limit=200)
        if wp.get("tracks"):
            name = (pl or {}).get("title") or f"YouTube Music Mix {pid[-6:]}"
            log("Note: this is an auto-generated YouTube Music mix; its contents change over time.")
            return name, wp["tracks"], True, ""
    hint = "If it's private, set it to Unlisted or add your YouTube Music login in Settings."
    if err:
        raise EngineError(f"Could not read playlist {pid}: {err}. {hint}")
    raise EngineError(f"Playlist {pid} returned no tracks. {hint}")


def _open_playlist(settings: Settings, pid: str, log=None):
    """fetch_raw with the configured YouTube Music login; if that login is broken or expired,
    retry without it so public / unlisted playlists and mixes keep syncing.
    Returns (ytm_client, title, raw_tracks, is_mix, cover_url)."""
    log = log or (lambda m="": None)
    try:
        ytm = _make_ytm(settings)
        return (ytm, *fetch_raw(ytm, pid, log))
    except Exception as e:
        if not settings.ytm_auth_path:
            raise
        log(f"YouTube Music login failed ({_redactor(settings)(e)}); retrying without login. "
            "If this keeps happening, paste fresh headers in Settings.")
        ytm = _make_ytm(replace(settings, ytm_auth_path=""))
        return (ytm, *fetch_raw(ytm, pid, log))


def read_playlist(settings: Settings, ref: str, log=None) -> tuple[str, bool, list[dict]]:
    """Returns (playlist name, is_mix, tracks) with tracks = [{"title", "artists", "album", "thumb", "video_id"}]."""
    return read_playlist_full(settings, ref, log)[:3]


def _song_version(ytm, title_clean: str, artists: list[str]):
    """Music videos / uploads often have no album: the YT Music song version of it, or None."""
    candidates = [artists[0]] + ([artists[1]] if len(artists) > 1 else [])
    for art in candidates:
        for r in ytm.search(f"{art} {clean(title_clean)}", filter="songs", limit=8):
            ra = [a["name"] for a in (r.get("artists") or [])]
            if (r.get("album") and sim(r["title"], title_clean) >= 0.85
                    and any(sim(x, y) >= 0.8 for x in ra for y in artists)):
                return r
    return None


def read_playlist_full(settings: Settings, ref: str, log=None,
                       cache: Cache | None = None) -> tuple[str, bool, list[dict], str]:
    """Like read_playlist, plus the playlist cover URL ('' if none): (name, is_mix, tracks, cover_url).
    Song-version lookups of album-less videos are remembered in `cache` (per video id)."""
    cache = cache if cache is not None else Cache()
    pid = parse_playlist_ref(ref)
    try:
        ytm, pl_title, raw, is_mix, cover = _open_playlist(settings, pid, log)
    except EngineError:
        raise
    except Exception as e:
        raise EngineError(f"Could not read playlist {pid}: {_redactor(settings)(e)}") from e
    cache.prefetch("ytm", [t.get("videoId") for t in raw])
    tracks = []
    for t in raw:
        artists = [a["name"].strip() for a in (t.get("artists") or []) if a.get("name")]
        album = (t.get("album") or {}).get("name")
        title = t.get("title") or ""
        video_id = t.get("videoId") or ""
        thumb = pick_thumb(t.get("thumbnails") or t.get("thumbnail"))  # playlist items / watch-queue items
        real_artist, title_clean = split_title(title, artists)
        if real_artist and not album:
            artists = [real_artist] + [a for a in artists if sim(a, real_artist) < 0.85]

        if not album and title_clean and artists:
            src = [title, artists]  # what YouTube said: a cached answer only counts while that is unchanged
            hit = cache.get("ytm", video_id) if video_id else None
            if hit and hit["src"] == src and (hit["album"] or Cache.recent(hit)):
                found = hit if hit["album"] else None
            else:
                try:
                    r, searched = _song_version(ytm, title_clean, artists), True
                except Exception:
                    r, searched = None, False  # don't remember a failed search
                found = r and {"album": r["album"]["name"], "title": r["title"],
                               "artists": [a["name"] for a in r["artists"]], "thumb": pick_thumb(r.get("thumbnails"))}
                if video_id and searched:
                    cache.put("ytm", video_id, {"src": src, **(found or {"album": None})})
            if found:
                album = found["album"]
                title_clean = found["title"]
                artists = found["artists"] or artists
                thumb = thumb or found["thumb"]
        tracks.append({"title": title_clean or title, "artists": artists, "album": album, "thumb": thumb,
                       "video_id": video_id})
    return pl_title, is_mix, tracks, cover


_COVERS: dict[str, str] = {}  # playlist id -> cover URL seen by the last successful check_ytm
_COVERS_MAX = 50


def take_cover(ref: str) -> str:
    """Cover URL that check_ytm found for this playlist ('' if none); each is handed out once."""
    try:
        return _COVERS.pop(parse_playlist_ref(ref), "")
    except EngineError:
        return ""


def check_ytm(settings: Settings, ref: str) -> tuple[bool, str, int]:
    """Cheap readability check (no per-track song lookups): (ok, playlist name or error, track count).
    The playlist cover is remembered for take_cover()."""
    try:
        pid = parse_playlist_ref(ref)
        _, title, raw, _, cover = _open_playlist(settings, pid)
        if len(_COVERS) >= _COVERS_MAX:
            _COVERS.clear()
        _COVERS[pid] = cover
        return True, title, len(raw)
    except EngineError as e:
        return False, _redactor(settings)(e), 0
    except Exception as e:
        return False, f"Could not read playlist: {_redactor(settings)(e)}", 0
