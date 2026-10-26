#!/usr/bin/env python3
"""
engine.py - YouTube Music playlist -> Lidarr (albums) + Jellyfin (playlist)

Refactor of the original ytm_sync.py CLI into an importable engine.  The matching
heuristics (clean / norm / sim / split_title, the YT Music song-search fallback, Lidarr
lookup thresholds, resolve() order incl. the MusicBrainz fallback, wrong-artist rejection,
Lidarr ensure/finalize, Jellyfin scoring and append-only playlist update) are unchanged;
only the plumbing differs: results are tracked per track, ``print`` became a ``log``
callback and ``sys.exit`` became ``EngineError``.

No web / database imports here.  Dependencies: requests, ytmusicapi.

CLI:
    python -m app.engine "https://music.youtube.com/playlist?list=PLxxxx" --dry-run
    python -m app.engine PLxxxx --only lidarr          (or --only jellyfin)

Environment variables (flags override them):
    LIDARR_URL, LIDARR_API_KEY, LIDARR_ROOT, LIDARR_QUALITY, LIDARR_METADATA,
    JELLYFIN_URL, JELLYFIN_API_KEY, JELLYFIN_USER, YTM_AUTH (path to browser.json),
    FINALIZE_TIMEOUT_MIN
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

import requests
from ytmusicapi import YTMusic

# ----------------------------------------------------------------- public types


class EngineError(Exception):
    """Fatal problem (bad URL/key, service unreachable, playlist unreadable).
    The message is safe to show to the user (no secrets)."""


@dataclass
class Settings:
    lidarr_url: str = ""
    lidarr_api_key: str = ""
    lidarr_root: str = ""        # "" = first in Lidarr
    lidarr_quality: str = ""
    lidarr_metadata: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    jellyfin_user: str = ""
    ytm_auth_path: str = ""      # optional browser.json for private playlists
    finalize_timeout_min: int = 20


@dataclass
class TrackResult:
    position: int
    title: str
    artists: list[str]
    album: str | None
    lidarr_status: str = "pending"    # added | monitored | downloaded | duplicate | not_found | error | skipped
    lidarr_release: str = ""          # e.g. "Calvin Harris - Motion [Album]"
    lidarr_note: str = ""             # e.g. "via MusicBrainz", error text
    jellyfin_status: str = "pending"  # in_playlist | added | duplicate | missing | not_added | skipped
    jellyfin_item_id: str = ""
    jellyfin_note: str = ""           # e.g. "same song as #3"
    thumb_url: str = ""               # YouTube Music thumbnail (source URL, served through /art)


@dataclass
class SyncResult:
    playlist_name: str
    is_mix: bool
    tracks: list[TrackResult]
    lidarr_found: int = 0
    lidarr_failed: int = 0
    jellyfin_matched: int = 0        # unique songs in the Jellyfin playlist
    jellyfin_added: int = 0
    jellyfin_duplicates: int = 0     # tracks that are the same Jellyfin song as an earlier track
    jellyfin_playlist_id: str = ""
    errors: list[str] = field(default_factory=list)
    status: str = "ok"               # ok | warning (some not found / missing) | error
    stopped: bool = False            # extra: should_stop() aborted the run early
    cover_url: str = ""              # YouTube Music playlist cover (source URL); "" for mixes


# ------------------------------------------------------- injectable time hooks
# Tests patch these (engine._sleep / engine._now) so waits run instantly.

def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> float:
    return time.time()


def _sleep_interruptible(seconds: float, should_stop) -> None:
    """Sleep in <=1 s slices so an app shutdown is noticed quickly."""
    remaining = float(seconds)
    while remaining > 0 and not should_stop():
        step = min(1.0, remaining)
        _sleep(step)
        remaining -= step


# ----------------------------------------------------------------- helpers

NOISE = re.compile(
    r"\s*[\(\[]\s*(feat\.?|ft\.?|featuring|with|prod\.?|official|lyric|audio|video|"
    r"visuali[sz]er|directed by|explicit|clean|remaster(ed)?|\d{4} remaster(ed)?|deluxe|"
    r"expanded|bonus|single|ep|radio edit|album version|live edit)[^\)\]]*[\)\]]",
    re.I,
)
# "(U.S. Version)", "(Bonus Edition)", "(International Deluxe)", "(2011 Remastered)" ...
EDITION = re.compile(r"\s*[\(\[][^\)\]]*\b(edition|version|deluxe|remaster(ed)?)\s*[\)\]]", re.I)
DASH_SUFFIX = re.compile(r"\s+-\s+(\d{4}\s+)?(remaster(ed)?|live|mono|stereo|radio edit|single version).*$", re.I)
PIPE_SUFFIX = re.compile(r"\s*[|｜].*$")
EMPTY_PARENS = re.compile(r"\s*[\(\[]\s*[\)\]]")
JUNK_WORDS = re.compile(r"\b(4k|hd|hq|official (music )?video|lyric video)\b", re.I)


def clean(s: str) -> str:
    """Strip editions, feat., video tags etc. but keep the original casing/characters."""
    s = PIPE_SUFFIX.sub("", s or "")
    s = EDITION.sub("", s)
    s = NOISE.sub("", s)
    s = DASH_SUFFIX.sub("", s)
    s = JUNK_WORDS.sub("", s)
    s = EMPTY_PARENS.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def norm(s: str) -> str:
    s = clean(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))  # é -> e, but keeps 中文 / 한국어
    s = s.lower().replace("&", " and ").replace("’", "'").replace("‐", "-")
    s = re.sub(r"[\W_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    # containment only counts if the strings are of similar length
    # ("The Album" must not match "Hit Collection, Volume 1: The Album New Edition")
    if short in long_ and len(short) / len(long_) >= 0.6:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def split_title(title: str, artists: list):
    """Video titles like 'Artist - Song | OFFICIAL VIDEO' -> (artist, 'Song').
    Returns (artist_or_None, cleaned_title)."""
    t = PIPE_SUFFIX.sub("", title or "").strip()
    found_artist = None
    # drop leading "<listed artist> - " (channel name repeated in the title), possibly twice
    for _ in range(2):
        if " - " not in t:
            break
        left, right = t.split(" - ", 1)
        if any(sim(left, a) > 0.85 for a in artists):
            t = right.strip()
        else:
            break
    # anything still in "X - Song" form: X is probably the real artist (e.g. uploader remixes)
    if " - " in t:
        left, right = t.split(" - ", 1)
        if len(left) < 60 and not DASH_SUFFIX.search(t):
            found_artist, t = left.strip(), right.strip()
    t = EMPTY_PARENS.sub("", JUNK_WORDS.sub("", t))
    return found_artist, re.sub(r"\s+", " ", t).strip(" -")


def parse_playlist_ref(url_or_id: str) -> str:
    """Extract the playlist id (the ``list=`` parameter of a URL, or the bare id)."""
    arg = (url_or_id or "").strip()
    m = re.search(r"[?&]list=([\w-]+)", arg)
    ref = m.group(1) if m else arg
    if not ref:
        raise EngineError("No playlist URL or ID given.")
    return ref


# ------------------------------------------------- secrets / error formatting

_QUERY = re.compile(r"(?<=[\w/])\?(?=[^\s)'\"]*=)[^\s)'\"]+")  # only real "?key=value" query strings


def _redactor(settings: Settings):
    """Returns f(text) that removes API keys and URL query strings from text."""
    secrets = [s for s in (settings.lidarr_api_key, settings.jellyfin_api_key) if s and len(s) >= 4]

    def redact(text) -> str:
        text = str(text)
        for sec in secrets:
            text = text.replace(sec, "***")
        return _QUERY.sub("", text)
    return redact


def _origin(url: str) -> str:
    """scheme://host:port of a URL (drops credentials, path and query)."""
    try:
        p = urlsplit(url if "://" in url else "http://" + url)
        host = p.hostname or url
        return f"{p.scheme}://{host}" + (f":{p.port}" if p.port else "")
    except ValueError:
        return "the configured URL"


def _base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


class ApiError(RuntimeError):
    """HTTP error from Lidarr / Jellyfin write calls (carries the status code)."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def _fatal(service: str, exc: Exception, url: str, redact) -> EngineError:
    """Turn any exception from connecting to a service into a clear, secret-free EngineError."""
    if isinstance(exc, EngineError):
        return exc
    status = getattr(exc, "status", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        word = "Unauthorized" if status == 401 else "Forbidden"
        return EngineError(f"{service}: {status} {word} — check the API key")
    if status == 404:
        return EngineError(f"{service}: 404 Not Found — check the URL")
    if status:
        return EngineError(f"{service}: HTTP {status} from {_origin(url)}")
    if isinstance(exc, requests.exceptions.Timeout):
        return EngineError(f"{service}: timed out connecting to {_origin(url)}")
    if isinstance(exc, requests.exceptions.ConnectionError):
        return EngineError(f"{service}: cannot reach {_origin(url)} — check the URL and that it is running")
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                        requests.exceptions.InvalidSchema)):
        return EngineError(f"{service}: invalid URL")
    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError, AttributeError)):
        # JSON decode error / unexpected JSON shape: answered, but not by the service we expect
        return EngineError(f"{service}: unexpected response from {_origin(url)} — is the URL right?")
    return EngineError(f"{service}: {redact(exc)}")


# ----------------------------------------------------------- YouTube Music

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
    """Returns (playlist name, is_mix, tracks) with tracks = [{"title", "artists", "album", "thumb"}]."""
    return read_playlist_full(settings, ref, log)[:3]


def read_playlist_full(settings: Settings, ref: str, log=None) -> tuple[str, bool, list[dict], str]:
    """Like read_playlist, plus the playlist cover URL ('' if none): (name, is_mix, tracks, cover_url)."""
    pid = parse_playlist_ref(ref)
    try:
        ytm, pl_title, raw, is_mix, cover = _open_playlist(settings, pid, log)
    except EngineError:
        raise
    except Exception as e:
        raise EngineError(f"Could not read playlist {pid}: {_redactor(settings)(e)}") from e
    tracks = []
    for t in raw:
        artists = [a["name"].strip() for a in (t.get("artists") or []) if a.get("name")]
        album = (t.get("album") or {}).get("name")
        title = t.get("title") or ""
        thumb = pick_thumb(t.get("thumbnails") or t.get("thumbnail"))  # playlist items / watch-queue items
        real_artist, title_clean = split_title(title, artists)
        if real_artist and not album:
            artists = [real_artist] + [a for a in artists if sim(a, real_artist) < 0.85]

        # Music videos / uploads often have no album: ask YT Music for the song version
        if not album and title_clean and artists:
            candidates = [artists[0]] + ([artists[1]] if len(artists) > 1 else [])
            try:
                for art in candidates:
                    res = ytm.search(f"{art} {clean(title_clean)}", filter="songs", limit=8)
                    hit = None
                    for r in res:
                        ra = [a["name"] for a in (r.get("artists") or [])]
                        if (r.get("album") and sim(r["title"], title_clean) >= 0.85
                                and any(sim(x, y) >= 0.8 for x in ra for y in artists)):
                            hit = r
                            break
                    if hit:
                        album = hit["album"]["name"]
                        title_clean = hit["title"]
                        artists = [a["name"] for a in hit["artists"]] or artists
                        thumb = thumb or pick_thumb(hit.get("thumbnails"))
                        break
            except Exception:
                pass
        tracks.append({"title": title_clean or title, "artists": artists, "album": album, "thumb": thumb})
    return pl_title, is_mix, tracks, cover


# ------------------------------------------------------------- MusicBrainz

_MB_LOCK = threading.Lock()
_MB_LAST = 0.0  # module-global rate limiter: MusicBrainz allows 1 request/second per client


def _mb_throttle() -> None:
    global _MB_LAST
    with _MB_LOCK:
        wait = 1.2 - (_now() - _MB_LAST)
        if wait > 0:
            _sleep(wait)
        _MB_LAST = _now()


class MusicBrainz:
    """Direct MusicBrainz search, used when Lidarr's own search fails or misses."""
    URL = "https://musicbrainz.org/ws/2"
    SKIP_TYPES = {"Compilation", "DJ-mix", "Live", "Mixtape/Street", "Remix", "Interview", "Spokenword"}

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "ytm_sync/1.0 (self-hosted YouTube Music -> Lidarr helper)"

    @staticmethod
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _search(self, entity, query):
        for attempt in range(5):
            _mb_throttle()
            r = self.s.get(f"{self.URL}/{entity}", params={"query": query, "fmt": "json", "limit": 25},
                           timeout=(10, 30))
            if r.status_code == 503 and attempt < 4:
                _sleep(5 * 2 ** attempt)  # 5, 10, 20, 40 s
                continue
            r.raise_for_status()
            return r.json()
        return {}

    @staticmethod
    def _artist_ok(credits, artists):
        names = [c.get("name") or (c.get("artist") or {}).get("name", "") for c in credits or []]
        return any(sim(n, a) >= 0.8 for n in names for a in artists)

    def album(self, artists, album):
        data = self._search("release-group", f"releasegroup:{self.q(clean(album))} AND artist:{self.q(artists[0])}")
        best, score = None, 0.0
        for rg in data.get("release-groups", []):
            if sim(rg.get("title", ""), album) < 0.85 or not self._artist_ok(rg.get("artist-credit"), artists):
                continue
            s = sim(rg["title"], album) - (0.5 if set(rg.get("secondary-types", [])) & self.SKIP_TYPES else 0)
            if s > score:
                best, score = rg["id"], s
        return best if score >= 0.85 else None

    def song(self, artists, title):
        """Release group (single preferred, else original album) containing this song."""
        wanted = clean(title)
        is_remix = bool(re.search(r"\b(remix|mix|edit|rework|bootleg)\b", title, re.I))
        data = self._search("recording", f"recording:{self.q(wanted)} AND artist:{self.q(artists[0])}")
        ranked = {}
        for rec in data.get("recordings", []):
            if sim(rec.get("title", ""), title) < 0.85 or not self._artist_ok(rec.get("artist-credit"), artists):
                continue
            for rel in rec.get("releases", []):
                rg = rel.get("release-group") or {}
                sec = set(rg.get("secondary-types", []))
                if sec & (self.SKIP_TYPES - ({"Remix"} if is_remix else set())):
                    continue
                ptype = rg.get("primary-type")
                if ptype == "Single" and sim(rg.get("title", ""), title) >= 0.85:
                    rank = 0
                elif ptype in ("Album", "EP"):
                    rank = 1
                elif ptype == "Single":
                    rank = 2
                else:
                    continue
                key = (rank, rel.get("status") != "Official", rel.get("date") or "9999")
                rid = rg.get("id")
                if rid and (rid not in ranked or key < ranked[rid]):
                    ranked[rid] = key
        return sorted(ranked, key=ranked.get)[:6]


# ------------------------------------------------------------------ Lidarr

_NOLOG = lambda m="": None  # noqa: E731


class Lidarr:
    CONNECT_TIMEOUT = 10

    def __init__(self, url, key, root=None, quality=None, metadata=None, dry=False, log=_NOLOG):
        self.url = _base_url(url) + "/api/v1"
        self.s = requests.Session()
        self.s.headers["X-Api-Key"] = key
        self.dry = dry
        self.log = log
        self.root = self._pick("rootfolder", root, "path")["path"]
        self.qp = self._pick("qualityprofile", quality, "name")["id"]
        mp = self._pick("metadataprofile", metadata, "name")
        self.mp = mp["id"]
        self.artists = {a["foreignArtistId"]: a for a in self.get("artist")}
        self._album_cache = {}
        self.pending = set()
        log(f"Lidarr: root={self.root} qualityProfile={self.qp} metadataProfile={mp.get('name')}, "
            f"{len(self.artists)} artists in library")
        allowed = {p["albumType"]["name"] for p in mp.get("primaryAlbumTypes", []) if p.get("allowed")}
        missing = {"Single", "EP"} - allowed
        if missing:
            log(f"  WARNING: metadata profile '{mp.get('name')}' doesn't allow {sorted(missing)}. Playlists are "
                f"mostly singles - Lidarr may drop those on the next artist refresh.")
            log("  Fix: Settings -> Profiles -> Metadata Profiles -> tick Single and EP "
                "(or choose a metadata profile that has them in this app's Settings).")

    def get(self, path, **params):
        # Lidarr's metadata server (api.lidarr.audio) often returns 503 under load -> retry with backoff
        delay = 3
        for attempt in range(3):
            r = self.s.get(f"{self.url}/{path}", params=params, timeout=(self.CONNECT_TIMEOUT, 90))
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                break
            _sleep(delay)
            delay *= 2
        r.raise_for_status()
        return r.json()

    def send(self, method, path, body):
        if self.dry:
            return {}
        r = self.s.request(method, f"{self.url}/{path}", json=body, timeout=(self.CONNECT_TIMEOUT, 120))
        if not r.ok:
            raise ApiError(f"{method} {path} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.text else {}

    def _pick(self, endpoint, wanted, field):
        items = self.get(endpoint)
        if not items:
            raise EngineError(f"Lidarr has no {endpoint} configured.")
        if wanted:
            for it in items:
                if str(it.get(field)).lower() == str(wanted).lower():
                    return it
            raise EngineError(f"Lidarr {endpoint} '{wanted}' not found. Options: {[i.get(field) for i in items]}")
        return items[0]

    def lookup(self, artists, album):
        """Best Lidarr/MusicBrainz release group for (any of artists, album), or None.
        Both the title and the artist have to match well on their own."""
        artists = [a for a in artists if a][:3]
        terms = []
        for t in (f"{artists[0]} {clean(album)}", f"{artists[0]} {album}"):
            if t not in terms:
                terms.append(t)
        best, score = None, 0.0
        for term in terms:
            for r in self.get("album/lookup", term=term)[:25]:
                a_name = (r.get("artist") or {}).get("artistName", "")
                t_sim = sim(r.get("title", ""), album)
                a_sim = max(sim(a_name, a) for a in artists)
                if t_sim < 0.85 or a_sim < 0.8:
                    continue
                s = t_sim * 0.6 + a_sim * 0.4
                if s > score:
                    best, score = r, s
            if score >= 0.95:
                break
            _sleep(0.3)
        return best

    def by_mbid(self, mbid):
        """Exact release-group lookup (doesn't use Lidarr's flaky text search)."""
        res = self.get("album/lookup", term=f"lidarr:{mbid}")
        for r in res:
            if r.get("foreignAlbumId") == mbid:
                return r
        return res[0] if res else None

    def existing_albums(self, artist_id):
        if artist_id not in self._album_cache:
            self._album_cache[artist_id] = {a["foreignAlbumId"]: a for a in self.get("album", artistId=artist_id)}
        return self._album_cache[artist_id]

    def ensure(self, found):
        """Make sure the album is in Lidarr, monitored and searched. Returns a status string."""
        fa_id = found["artist"]["foreignArtistId"]
        artist = self.artists.get(fa_id)

        if artist:  # artist already in Lidarr -> monitor this one album
            alb = self.existing_albums(artist["id"]).get(found["foreignAlbumId"])
            if alb is None:
                # album not in artist's list (filtered by metadata profile?) -> add it directly
                return self._add_album(found, existing_artist=artist)
            if alb.get("monitored") and (alb.get("statistics") or {}).get("percentOfTracks", 0) >= 100:
                return "already downloaded"
            if not alb.get("monitored"):
                self.send("PUT", "album/monitor", {"albumIds": [alb["id"]], "monitored": True})
            if not artist.get("monitored"):
                artist["monitored"] = True
                self.send("PUT", f"artist/{artist['id']}", artist)
            self.send("POST", "command", {"name": "AlbumSearch", "albumIds": [alb["id"]]})
            return "monitored + search started"

        return self._add_album(found)

    def _add_album(self, found, existing_artist=None):
        body = dict(found)
        body["monitored"] = True
        body["addOptions"] = {"searchForNewAlbum": True}
        if existing_artist:
            body["artist"] = existing_artist
            body["artistId"] = existing_artist["id"]
        else:
            art = dict(found["artist"])
            art.update({
                "qualityProfileId": self.qp,
                "metadataProfileId": self.mp,
                "rootFolderPath": self.root,
                "monitored": True,
                "monitorNewItems": "none",
                "addOptions": {"monitor": "none", "searchForMissingAlbums": False,
                               "albumsToMonitor": [found["foreignAlbumId"]]},
            })
            body["artist"] = art
        res = self.send("POST", "album", body)
        self.pending.add(found["foreignAlbumId"])
        if not self.dry and not existing_artist and res.get("artist"):
            self.artists[found["artist"]["foreignArtistId"]] = res["artist"]
        if not self.dry and res.get("artistId"):
            self._album_cache.pop(res["artistId"], None)
        return "added" if not existing_artist else "added to existing artist"

    def finalize(self, timeout_min=20, should_stop=lambda: False):
        """Newly added artists get refreshed in the background, and that refresh applies
        'monitor: none' to their albums - including the one we wanted. So wait until Lidarr
        is done, then monitor our albums and search for them.

        Returns the set of foreign album ids that were not visible in Lidarr yet (or None
        if nothing had to be done / the wait was interrupted)."""
        if self.dry or not self.pending:
            return None
        log = self.log
        log("")
        log(f"Waiting for Lidarr to finish importing new artists (up to {timeout_min} min)...")
        busy_names = {"RefreshArtist", "BulkRefreshArtist", "RefreshAlbum", "AddArtist"}
        deadline = _now() + timeout_min * 60
        _sleep_interruptible(10, should_stop)
        while _now() < deadline and not should_stop():
            busy = [c for c in self.get("command")
                    if c.get("name") in busy_names and c.get("status") in ("queued", "started")]
            if not busy:
                break
            log(f"  ...{len(busy)} refresh task(s) still running")
            _sleep_interruptible(15, should_stop)
        if should_stop():
            log("Stopped while waiting for Lidarr; albums will be picked up on the next sync.")
            return None

        by_foreign = {a["foreignAlbumId"]: a for a in self.get("album")}
        ids, missing = [], []
        for fid in self.pending:
            alb = by_foreign.get(fid)
            (ids.append(alb["id"]) if alb else missing.append(fid))
        unmonitored = [by_foreign[f]["id"] for f in self.pending if f in by_foreign and not by_foreign[f].get("monitored")]
        if unmonitored:
            self.send("PUT", "album/monitor", {"albumIds": unmonitored, "monitored": True})
        for k in range(0, len(ids), 50):
            self.send("POST", "command", {"name": "AlbumSearch", "albumIds": ids[k:k + 50]})
        log(f"Lidarr: {len(ids)} album(s) monitored and searched ({len(unmonitored)} had been unmonitored by Lidarr).")
        if missing:
            log(f"  {len(missing)} album(s) not visible in Lidarr yet - they will be picked up on the next sync.")
        return set(missing)


def resolve(lid, mb, a):
    """Find the Lidarr release for one album group. Returns (found, how, last_error)."""
    artists, album, titles = a["artists"], a["album"], a["titles"]
    err = None

    def attempt(fn, *fargs):
        nonlocal err
        try:
            return fn(*fargs)
        except Exception as e:
            err = str(e).split(" for url")[0]
            return None

    # 1. Lidarr text search: album, then the song as a single
    if album:
        found = attempt(lid.lookup, artists, album)
        if found:
            return found, "", None
    for t in titles:
        found = attempt(lid.lookup, artists, clean(t))
        if found:
            return found, f" (via song '{clean(t)}')", None
    # 2. MusicBrainz directly, then exact id lookup in Lidarr
    if album:
        mbid = attempt(mb.album, artists, album)
        if mbid:
            found = attempt(lid.by_mbid, mbid)
            if found:
                return found, " (via MusicBrainz)", None
    for t in titles:
        fallback = None
        for mbid in attempt(mb.song, artists, t) or []:
            found = attempt(lid.by_mbid, mbid)
            if not found:
                continue
            name = (found.get("artist") or {}).get("artistName", "")
            if norm(name) == "various artists":
                continue
            if any(sim(name, x) >= 0.8 for x in artists):
                return found, f" (via MusicBrainz, song '{clean(t)}')", None
            fallback = fallback or found  # e.g. "KPop Demon Hunters Cast" for HUNTR/X
        if fallback:
            return fallback, f" (via MusicBrainz, song '{clean(t)}', different artist name)", None
    return None, "", err


_ENSURE_STATUS = {
    "already downloaded": ("downloaded", ""),
    "monitored + search started": ("monitored", "search started"),
    "added": ("added", ""),
    "added to existing artist": ("added", "added to existing artist"),
}


def _join_note(*parts) -> str:
    return "; ".join(p for p in parts if p)


def _sync_lidarr(lid: Lidarr, tracks: list[dict], results: list[TrackResult], dry: bool,
                 log, redact, timeout_min: int, should_stop) -> bool:
    """Runs the Lidarr phase and fills the TrackResults. Returns True if it was stopped early."""
    # group tracks by album; tracks without album are tried as singles
    albums = {}
    for t, tr in zip(tracks, results):
        if not t["artists"]:
            tr.lidarr_status = "not_found"
            tr.lidarr_note = "no artist on YouTube, add manually"
            log(f"  ?  {t['title']}  - no artist information on YouTube, add manually")
            continue
        key = (norm(t["artists"][0]), norm(t["album"]) if t["album"] else "§" + norm(t["title"]))
        albums.setdefault(key, {"artists": t["artists"], "album": t["album"], "titles": [], "results": []})
        albums[key]["titles"].append(t["title"])
        albums[key]["results"].append(tr)

    log("")
    log(f"== Lidarr: {len(albums)} albums/singles to check ==")
    mb = MusicBrainz()
    tag = "[dry-run] " if dry else ""
    done, failed = set(), []

    def mark(a, status, release="", note=""):
        for tr in a["results"]:
            tr.lidarr_status, tr.lidarr_release, tr.lidarr_note = status, release, note

    by_release: dict[str, list] = {}  # foreignAlbumId -> groups that ended up there
    for a in albums.values():
        if should_stop():
            log("Stopped.")
            return True
        artists, album = a["artists"], a["album"]
        label = f"{artists[0]} - {album}" if album else f"{artists[0]} - {a['titles'][0]} (no album on YouTube)"
        try:
            found, how, err = resolve(lid, mb, a)
            if not found:
                failed.append(label)
                if err:
                    log(f"  ?  {label}  - lookup failed: {redact(err)}")
                    mark(a, "error", "", f"lookup failed: {redact(err)}")
                else:
                    log(f"  ?  {label}  - not on MusicBrainz, add manually")
                    mark(a, "not_found", "", "not on MusicBrainz, add manually")
                continue
            kind = found.get("albumType") or "?"
            release = f"{found['artist']['artistName']} - {found['title']} [{kind}]"
            how_note = how.strip().strip("()")
            if found["foreignAlbumId"] in done:
                log(f"  =  {label}  -> same release as above, skipped")
                mark(a, "duplicate", release, _join_note(how_note, "same release as another track"))
                by_release.setdefault(found["foreignAlbumId"], []).append(a)
                continue
            done.add(found["foreignAlbumId"])
            status = lid.ensure(found)
            log(f"  ✓  {label}  ->  {release}{how}: {tag}{status}")
            st, extra = _ENSURE_STATUS.get(status, ("added", status))
            mark(a, st, release, _join_note(how_note, extra))
            by_release.setdefault(found["foreignAlbumId"], []).append(a)
        except Exception as e:
            failed.append(label)
            log(f"  !  {label}: {redact(e)}")
            mark(a, "error", "", redact(e))

    if failed:
        log("")
        log(f"  {len(failed)} item(s) need manual adding in Lidarr (see '?' / '!' above).")
    missing = lid.finalize(timeout_min, should_stop)
    if missing:
        for fid in missing:
            for a in by_release.get(fid, []):
                for tr in a["results"]:
                    tr.lidarr_note = _join_note(tr.lidarr_note, "not visible in Lidarr yet, next sync will finish it")
    return bool(should_stop())  # True if the finalize wait was interrupted


# ---------------------------------------------------------------- Jellyfin

class Jellyfin:
    CONNECT_TIMEOUT = 10

    def __init__(self, url, key, user, dry=False, log=_NOLOG):
        self.url = _base_url(url)
        self.s = requests.Session()
        self.s.headers["Authorization"] = f'MediaBrowser Token="{key}"'
        self.dry = dry
        users = self.req("GET", "/Users")
        match = [u for u in users if not user or u["Name"].lower() == user.lower()]
        if not match:
            raise EngineError(f"Jellyfin user '{user}' not found. Users: {[u['Name'] for u in users]}")
        self.uid = match[0]["Id"]
        self.user_name = match[0]["Name"]
        log(f"Jellyfin: user={match[0]['Name']}")

    def req(self, method, path, **kw):
        r = self.s.request(method, self.url + path, timeout=(self.CONNECT_TIMEOUT, 60), **kw)
        if not r.ok:
            raise ApiError(f"{method} {path} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.text else {}

    def find_track(self, t):
        """Best library item for the track, or None. Like Lidarr.lookup, the title and the artist have to
        match well on their own: a different song by the same artist, or a cover of the same song, is no match."""
        title = t["title"]
        artists = [a for a in t["artists"] if a]
        queries = [title]
        cleaned = NOISE.sub("", DASH_SUFFIX.sub("", title)).strip()
        if cleaned and cleaned != title:
            queries.append(cleaned)
        best, score = None, 0.0
        for q in queries:
            items = self.req("GET", "/Items", params={
                "userId": self.uid, "includeItemTypes": "Audio", "recursive": "true",
                "searchTerm": q, "limit": 50, "fields": "Album,Artists",
            }).get("Items", [])
            for it in items:
                arts = (it.get("Artists") or []) + [it.get("AlbumArtist") or ""]
                t_sim = sim(it.get("Name", ""), title)
                a_score = max([sim(a, b) for a in arts if a for b in artists] or [0])
                if t_sim < 0.85 or a_score < 0.8:
                    continue
                s = t_sim * 0.6 + a_score * 0.3
                if t["album"] and it.get("Album"):
                    s += sim(it["Album"], t["album"]) * 0.1
                else:
                    s += 0.05
                if s > score:
                    best, score = it, s
            if score >= 0.9:
                break
        return best if score >= 0.75 else None

    def playlist_item_ids(self, playlist_id) -> set:
        return {i["Id"] for i in self.req("GET", f"/Playlists/{playlist_id}/Items",
                                          params={"userId": self.uid}).get("Items", [])}

    def ensure_playlist(self, name, ids):
        """Append-only. Returns (message, playlist_id, new_ids, lost_ids): new_ids are the items that were
        (or, in dry-run, would be) added; lost_ids were sent but are not in the playlist when it is read back."""
        pls = self.req("GET", "/Items", params={
            "userId": self.uid, "includeItemTypes": "Playlist", "recursive": "true", "searchTerm": name,
        }).get("Items", [])
        pl = next((p for p in pls if p["Name"] == name), None)
        if not pl:
            if self.dry:
                return f"would create playlist '{name}' with {len(ids)} tracks", "", list(ids), []
            res = self.req("POST", "/Playlists", json={
                "Name": name, "Ids": ids, "UserId": self.uid, "MediaType": "Audio",
            })
            pl_id, new = res.get("Id") or "", list(ids)
            msg = f"created playlist '{name}' with {len(ids)} tracks (id {res.get('Id')})"
        else:
            pl_id = pl["Id"]
            present = self.playlist_item_ids(pl_id)
            new = [i for i in ids if i not in present]
            if new and not self.dry:
                for k in range(0, len(new), 50):
                    self.req("POST", f"/Playlists/{pl_id}/Items",
                             params={"ids": ",".join(new[k:k + 50]), "userId": self.uid})
            verb = "would add" if self.dry else "added"
            msg = f"playlist '{name}' exists: {verb} {len(new)} new track(s)"
        lost = []
        if new and pl_id and not self.dry:
            # Jellyfin can answer a write with success and still not keep the items: check
            present = self.playlist_item_ids(pl_id)
            lost = [i for i in new if i not in present]
            new = [i for i in new if i in present]
        return msg, pl_id, new, lost


def _sync_jellyfin(jf: Jellyfin, name: str, tracks: list[dict], results: list[TrackResult],
                   log, redact, should_stop, errors: list[str]) -> str:
    """Runs the Jellyfin phase. Returns the playlist id ('' if none)."""
    log("")
    log(f"== Jellyfin: matching {len(tracks)} tracks ==")
    ids, missing = [], []
    matched: list[tuple[TrackResult, str]] = []
    duplicates: list[TrackResult] = []
    first_pos: dict[str, int] = {}  # Jellyfin item id -> position of the first track matched to it
    consecutive_errors = 0
    for i, (t, tr) in enumerate(zip(tracks, results)):
        if should_stop():
            log("Stopped.")
            return ""
        try:
            it = jf.find_track(t)
            consecutive_errors = 0
        except Exception as e:
            log(f"  !  {t['title']}: {redact(e)}")
            consecutive_errors += 1
            if consecutive_errors >= 5:
                errors.append("Jellyfin: stopped after repeated errors: " + redact(e))
                log("  Jellyfin keeps failing, giving up on the Jellyfin step.")
                for r in results[i:]:
                    r.jellyfin_status = "skipped"
                return ""
            tr.jellyfin_status = "missing"
            missing.append(t)
            continue
        if not it:
            tr.jellyfin_status = "missing"
            missing.append(t)
            continue
        tr.jellyfin_item_id = it["Id"]
        if it["Id"] in first_pos:
            # the playlist holds each song once: this track is not counted as a song of its own
            tr.jellyfin_status = "duplicate"
            tr.jellyfin_note = f"same song as #{first_pos[it['Id']]}"
            duplicates.append(tr)
            continue
        first_pos[it["Id"]] = tr.position
        ids.append(it["Id"])
        matched.append((tr, it["Id"]))
    log(f"  matched {len(ids)}/{len(tracks)}")
    if duplicates:
        log(f"  {len(duplicates)} track(s) are the same Jellyfin song as an earlier track (added once):")
        for tr in duplicates:
            log(f"    = #{tr.position} {', '.join(tr.artists)} - {tr.title}  ({tr.jellyfin_note})")
    playlist_id = ""
    if ids:
        try:
            msg, playlist_id, new_ids, lost_ids = jf.ensure_playlist(name, ids)
            log("  " + msg)
        except Exception as e:
            errors.append("Jellyfin playlist update failed: " + redact(e))
            log(f"  !  playlist update failed: {redact(e)}")
            for tr in [tr for tr, _ in matched] + duplicates:
                tr.jellyfin_status = "skipped"
            return ""
        new, lost = set(new_ids), set(lost_ids)
        for tr, item_id in matched:
            tr.jellyfin_status = "not_added" if item_id in lost else "added" if item_id in new else "in_playlist"
        if lost:
            errors.append(f"Jellyfin accepted {len(lost)} track(s) for playlist '{name}' but did not keep them")
            log(f"  !  {len(lost)} track(s) are not in the playlist after adding them:")
            for tr, item_id in matched:
                if item_id in lost:
                    log(f"    - #{tr.position} {', '.join(tr.artists)} - {tr.title}")
    if missing:
        log(f"  not in library yet ({len(missing)}) - will be appended on a later sync once Lidarr has them:")
        for t in missing:
            log(f"    - {', '.join(t['artists'])} - {t['title']}" + (f"  [{t['album']}]" if t['album'] else ""))
    return playlist_id


# ------------------------------------------------------------ public: sync

def sync_playlist(settings: Settings, ref: str, *, jellyfin_name: str | None = None,
                  do_lidarr: bool = True, do_jellyfin: bool = True, dry_run: bool = False,
                  log=print, should_stop=lambda: False) -> SyncResult:
    """Sync one YouTube Music playlist. Raises EngineError for fatal problems (service
    unreachable / unauthorised, playlist unreadable); per-track problems end up in the result."""
    redact = _redactor(settings)

    def say(msg=""):
        log(redact(msg))

    if do_lidarr and not (settings.lidarr_url and settings.lidarr_api_key):
        raise EngineError("Lidarr: URL and API key are not configured (see Settings).")
    if do_jellyfin and not (settings.jellyfin_url and settings.jellyfin_api_key):
        raise EngineError("Jellyfin: URL and API key are not configured (see Settings).")

    # Connect to the services first (cheap) so a bad key fails before the slow playlist read.
    lid = jf = None
    if do_lidarr:
        try:
            lid = Lidarr(settings.lidarr_url, settings.lidarr_api_key, settings.lidarr_root or None,
                         settings.lidarr_quality or None, settings.lidarr_metadata or None, dry_run, say)
        except Exception as e:
            raise _fatal("Lidarr", e, settings.lidarr_url, redact) from None
    if do_jellyfin:
        try:
            jf = Jellyfin(settings.jellyfin_url, settings.jellyfin_api_key, settings.jellyfin_user,
                          dry_run, say)
        except Exception as e:
            raise _fatal("Jellyfin", e, settings.jellyfin_url, redact) from None

    title, is_mix, tracks, cover = read_playlist_full(settings, ref, say)
    say(f"YouTube Music playlist '{title}': {len(tracks)} tracks")

    results = [TrackResult(position=i, title=t["title"], artists=list(t["artists"]), album=t["album"],
                           thumb_url=t.get("thumb") or "")
               for i, t in enumerate(tracks, 1)]
    res = SyncResult(playlist_name=title, is_mix=is_mix, tracks=results, cover_url=cover)

    stopped = False
    if do_lidarr:
        stopped = _sync_lidarr(lid, tracks, results, dry_run, say, redact,
                               settings.finalize_timeout_min, should_stop)
    else:
        for tr in results:
            tr.lidarr_status = "skipped"
    if do_jellyfin and not stopped:
        res.jellyfin_playlist_id = _sync_jellyfin(jf, jellyfin_name or title, tracks, results,
                                                  say, redact, should_stop, res.errors)
        stopped = stopped or should_stop()
    elif not do_jellyfin:
        for tr in results:
            tr.jellyfin_status = "skipped"

    res.lidarr_found = sum(tr.lidarr_status in ("added", "monitored", "downloaded", "duplicate") for tr in results)
    res.lidarr_failed = sum(tr.lidarr_status in ("not_found", "error") for tr in results)
    res.jellyfin_matched = sum(tr.jellyfin_status in ("in_playlist", "added") for tr in results)
    res.jellyfin_added = len({tr.jellyfin_item_id for tr in results if tr.jellyfin_status == "added"})
    res.jellyfin_duplicates = sum(tr.jellyfin_status == "duplicate" for tr in results)
    res.stopped = stopped
    if stopped:
        res.errors.append("Stopped before completion.")
    if res.errors and not stopped and any(e.startswith("Jellyfin") for e in res.errors):
        res.status = "error"
    elif res.lidarr_failed or stopped or any(tr.jellyfin_status == "missing" for tr in results):
        res.status = "warning"
    else:
        res.status = "ok"
    say("")
    say(f"Done: Lidarr {res.lidarr_found}/{len(results)}, Jellyfin {res.jellyfin_matched}/{len(results)} "
        f"({res.jellyfin_added} added) - {res.status}")
    return res


# ------------------------------------------------------ public: connection checks

_CHECK_TIMEOUT = (5, 15)


def _lidarr_session(settings: Settings):
    s = requests.Session()
    s.headers["X-Api-Key"] = settings.lidarr_api_key
    return s, _base_url(settings.lidarr_url) + "/api/v1"


def check_lidarr(settings: Settings) -> tuple[bool, str]:
    if not (settings.lidarr_url and settings.lidarr_api_key):
        return False, "Not configured"
    redact = _redactor(settings)
    try:
        s, base = _lidarr_session(settings)
        r = s.get(f"{base}/system/status", timeout=_CHECK_TIMEOUT)
        r.raise_for_status()
        version = r.json().get("version", "?")
        r = s.get(f"{base}/artist", timeout=_CHECK_TIMEOUT)
        r.raise_for_status()
        n = len(r.json())
        return True, f"Connected · v{version} · {n} artist{'s' if n != 1 else ''}"
    except Exception as e:
        return False, str(_fatal("Lidarr", e, settings.lidarr_url, redact)).removeprefix("Lidarr: ")


def lidarr_options(settings: Settings) -> dict:
    """Names to offer in the settings form. Empty lists if Lidarr is not configured / reachable."""
    out = {"root": [], "quality": [], "metadata": []}
    if not (settings.lidarr_url and settings.lidarr_api_key):
        return out
    try:
        s, base = _lidarr_session(settings)
        for key, endpoint, fld in (("root", "rootfolder", "path"), ("quality", "qualityprofile", "name"),
                                   ("metadata", "metadataprofile", "name")):
            r = s.get(f"{base}/{endpoint}", timeout=_CHECK_TIMEOUT)
            r.raise_for_status()
            out[key] = [str(i[fld]) for i in r.json() if i.get(fld) is not None]
    except Exception:
        return {"root": [], "quality": [], "metadata": []}
    return out


def check_jellyfin(settings: Settings) -> tuple[bool, str]:
    if not (settings.jellyfin_url and settings.jellyfin_api_key):
        return False, "Not configured"
    redact = _redactor(settings)
    try:
        s = requests.Session()
        s.headers["Authorization"] = f'MediaBrowser Token="{settings.jellyfin_api_key}"'
        r = s.get(_base_url(settings.jellyfin_url) + "/Users", timeout=_CHECK_TIMEOUT)
        r.raise_for_status()
        users = r.json()
        user = settings.jellyfin_user
        match = [u for u in users if not user or u["Name"].lower() == user.lower()]
        if not match:
            return False, f"user '{user}' not found. Users: {[u['Name'] for u in users]}"
        return True, f"Connected · user {match[0]['Name']}"
    except Exception as e:
        return False, str(_fatal("Jellyfin", e, settings.jellyfin_url, redact)).removeprefix("Jellyfin: ")


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


# -------------------------------------------------------------------- CLI

def settings_from_env(env=None) -> Settings:
    e = (env if env is not None else os.environ).get
    try:
        timeout = int(e("FINALIZE_TIMEOUT_MIN") or 20)
    except ValueError:
        timeout = 20
    return Settings(
        lidarr_url=e("LIDARR_URL") or "", lidarr_api_key=e("LIDARR_API_KEY") or "",
        lidarr_root=e("LIDARR_ROOT") or "", lidarr_quality=e("LIDARR_QUALITY") or "",
        lidarr_metadata=e("LIDARR_METADATA") or "",
        jellyfin_url=e("JELLYFIN_URL") or "", jellyfin_api_key=e("JELLYFIN_API_KEY") or "",
        jellyfin_user=e("JELLYFIN_USER") or "", ytm_auth_path=e("YTM_AUTH") or "",
        finalize_timeout_min=timeout,
    )


def main(argv=None) -> int:
    base = settings_from_env()
    p = argparse.ArgumentParser(
        prog="python -m app.engine",
        description="Sync a YouTube Music playlist to Lidarr and Jellyfin (config from env vars: "
                    "LIDARR_URL, LIDARR_API_KEY, LIDARR_ROOT, LIDARR_QUALITY, LIDARR_METADATA, "
                    "JELLYFIN_URL, JELLYFIN_API_KEY, JELLYFIN_USER, YTM_AUTH, FINALIZE_TIMEOUT_MIN)")
    p.add_argument("playlist", help="YouTube Music playlist URL or ID")
    p.add_argument("--name", help="Jellyfin playlist name (default: same as on YouTube Music)")
    p.add_argument("--only", choices=["lidarr", "jellyfin"])
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("--ytm-auth", default=base.ytm_auth_path)
    p.add_argument("--lidarr-url", default=base.lidarr_url)
    p.add_argument("--lidarr-key", default=base.lidarr_api_key)
    p.add_argument("--lidarr-root", default=base.lidarr_root)
    p.add_argument("--lidarr-quality", default=base.lidarr_quality)
    p.add_argument("--lidarr-metadata", default=base.lidarr_metadata)
    p.add_argument("--jellyfin-url", default=base.jellyfin_url)
    p.add_argument("--jellyfin-key", default=base.jellyfin_api_key)
    p.add_argument("--jellyfin-user", default=base.jellyfin_user)
    p.add_argument("--finalize-timeout", type=int, default=base.finalize_timeout_min,
                   help="minutes to wait for Lidarr to import new artists (default 20)")
    args = p.parse_args(argv)

    settings = Settings(
        lidarr_url=args.lidarr_url, lidarr_api_key=args.lidarr_key, lidarr_root=args.lidarr_root,
        lidarr_quality=args.lidarr_quality, lidarr_metadata=args.lidarr_metadata,
        jellyfin_url=args.jellyfin_url, jellyfin_api_key=args.jellyfin_key,
        jellyfin_user=args.jellyfin_user, ytm_auth_path=args.ytm_auth,
        finalize_timeout_min=args.finalize_timeout,
    )
    try:
        sync_playlist(settings, args.playlist, jellyfin_name=args.name,
                      do_lidarr=args.only != "jellyfin", do_jellyfin=args.only != "lidarr",
                      dry_run=args.dry_run, log=lambda m="": print(m, flush=True))
    except EngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
