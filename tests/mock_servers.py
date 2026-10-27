"""Fake Lidarr + Jellyfin HTTP servers (127.0.0.1, random port, background thread) and
stubs for the two things that are unreachable from tests: YouTube Music and MusicBrainz.

    lidarr = MockLidarr(api_key="lk").start()
    lidarr.add_release("Motion", "Calvin Harris", "alb-motion", "art-ch")
    ...
    lidarr.writes  -> [("POST", "/api/v1/album", {...}), ...]   (recorded write calls)
    lidarr.stop()
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import re
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse


class _Server:
    """Base: runs a ThreadingHTTPServer on a random localhost port."""

    def __init__(self):
        self.lock = threading.RLock()
        self.writes: list[tuple[str, str, object]] = []   # (method, path, json body) of POST/PUT/DELETE
        self.requests: list[tuple[str, str]] = []         # (method, path+query) of everything
        self.headers_seen: list[dict] = []
        self._httpd = None
        self._thread = None

    # subclasses implement: handle(method, path, query, body, headers) -> (status, obj)
    def handle(self, method, path, query, body, headers):  # pragma: no cover
        raise NotImplementedError

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                query = {k: v[0] for k, v in parse_qs(u.query).items()}
                with outer.lock:
                    outer.requests.append((method, self.path))
                    outer.headers_seen.append(dict(self.headers))
                    status, obj = outer.handle(method, u.path, query, body, self.headers)
                    if method != "GET" and status < 400:
                        outer.writes.append((method, u.path, body if body is not None else query))
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PUT(self):
                self._do("PUT")

            def do_DELETE(self):
                self._do("DELETE")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.01), daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def clear_writes(self):
        with self.lock:
            self.writes.clear()

    def writes_to(self, method, path_substr):
        return [w for w in self.writes if w[0] == method and path_substr in w[1]]


# ------------------------------------------------------------------- Lidarr

def _metadata_profile(name="Standard", types=("Album", "EP", "Single")):
    all_types = ["Album", "EP", "Single", "Broadcast", "Other"]
    return {"id": 1, "name": name,
            "primaryAlbumTypes": [{"albumType": {"name": t}, "allowed": t in types} for t in all_types]}


class MockLidarr(_Server):
    """Minimal Lidarr v1 API.

    State (all plain attributes, tests may edit them freely):
      artists / albums           library contents (dicts shaped like Lidarr's)
      lookups                    {search term: [release, ...]} for GET album/lookup?term=...
                                 ("lidarr:<mbid>" terms are registered automatically by add_release)
      refresh_unmonitors         POST album for a *new* artist stores the album as unmonitored,
                                 simulating Lidarr's background refresh applying "monitor: none"
      busy_polls                 number of GET command polls that still report a running RefreshArtist
      hide_added_albums          POST album does not create the album record (not visible yet)
      writes                     recorded POST/PUT/DELETE calls (method, path, body)
    """

    def __init__(self, api_key="lidarr-key"):
        super().__init__()
        self.api_key = api_key
        self.version = "2.5.3.4341"
        self.root_folders = [{"id": 1, "path": "/music"}, {"id": 2, "path": "/music2"}]
        self.quality_profiles = [{"id": 1, "name": "Any"}, {"id": 2, "name": "Lossless"}]
        self.metadata_profiles = [_metadata_profile("Standard"), dict(_metadata_profile("Albums only", ("Album",)), id=2)]
        self.artists: list[dict] = []
        self.albums: list[dict] = []
        self.lookups: dict[str, list] = {}
        self.refresh_unmonitors = True
        self.busy_polls = 0
        self.hide_added_albums = False
        self.fail_paths: dict[str, int] = {}   # path substring -> HTTP status to return
        self.error_text = "boom"               # body text of those failures (tests make it echo the key)
        self._next_id = 100

    # ---- state builders
    def _id(self):
        self._next_id += 1
        return self._next_id

    def add_release(self, title, artist, album_id, artist_id, album_type="Album", terms=None):
        """Register a release the (fake) metadata server knows about. Returns the lookup dict."""
        rel = {"title": title, "foreignAlbumId": album_id, "albumType": album_type,
               "artist": {"artistName": artist, "foreignArtistId": artist_id}}
        for term in (terms if terms is not None else [f"{artist} {title}"]):
            self.lookups.setdefault(term, []).append(rel)
        self.lookups.setdefault(f"lidarr:{album_id}", []).append(rel)
        return rel

    def add_library_artist(self, name, foreign_id, monitored=True):
        a = {"id": self._id(), "artistName": name, "foreignArtistId": foreign_id, "monitored": monitored}
        self.artists.append(a)
        return a

    def add_library_album(self, artist, title, foreign_id, monitored=False, percent=0):
        alb = {"id": self._id(), "artistId": artist["id"], "title": title, "foreignAlbumId": foreign_id,
               "monitored": monitored, "statistics": {"percentOfTracks": percent}}
        self.albums.append(alb)
        return alb

    def album_by_foreign(self, foreign_id):
        return next((a for a in self.albums if a["foreignAlbumId"] == foreign_id), None)

    def searched_album_ids(self):
        ids = []
        for m, p, b in self.writes:
            if m == "POST" and p.endswith("/command") and isinstance(b, dict) and b.get("name") == "AlbumSearch":
                ids += b["albumIds"]
        return ids

    # ---- HTTP
    def handle(self, method, path, q, body, headers):
        if headers.get("X-Api-Key") != self.api_key:
            return 401, {"error": "Unauthorized"}
        for sub, st in self.fail_paths.items():
            if sub in path:
                return st, {"error": self.error_text}
        p = path.removeprefix("/api/v1/")
        if method == "GET":
            if p == "system/status":
                return 200, {"version": self.version, "appName": "Lidarr"}
            if p == "rootfolder":
                return 200, self.root_folders
            if p == "qualityprofile":
                return 200, self.quality_profiles
            if p == "metadataprofile":
                return 200, self.metadata_profiles
            if p == "artist":
                return 200, self.artists
            if p == "album":
                if "artistId" in q:
                    return 200, [a for a in self.albums if a["artistId"] == int(q["artistId"])]
                if "foreignAlbumId" in q:
                    return 200, [a for a in self.albums if a["foreignAlbumId"] == q["foreignAlbumId"]]
                return 200, self.albums
            if p == "album/lookup":
                return 200, self.lookups.get(unquote(q.get("term", "")), [])
            if p == "command":
                if self.busy_polls > 0:
                    self.busy_polls -= 1
                    return 200, [{"name": "RefreshArtist", "status": "started"}]
                return 200, [{"name": "RefreshArtist", "status": "completed"}]
            return 404, {}
        if method == "POST" and p == "album":
            return 201, self._post_album(body)
        if method == "POST" and p == "command":
            return 201, {"id": self._id(), "name": body.get("name"), "status": "queued"}
        if method == "PUT" and p == "album/monitor":
            for a in self.albums:
                if a["id"] in body["albumIds"]:
                    a["monitored"] = body["monitored"]
            return 202, {}
        m = re.fullmatch(r"artist/(\d+)", p)
        if method == "PUT" and m:
            for a in self.artists:
                if a["id"] == int(m.group(1)):
                    a.update(body)
            return 202, body
        return 404, {}

    def _post_album(self, body):
        art = body["artist"]
        artist = next((a for a in self.artists if a["foreignArtistId"] == art["foreignArtistId"]), None)
        is_new_artist = artist is None
        if is_new_artist:
            artist = {"id": self._id(), "artistName": art.get("artistName"),
                      "foreignArtistId": art["foreignArtistId"], "monitored": art.get("monitored", True)}
            self.artists.append(artist)
        if not self.hide_added_albums:
            self.albums.append({
                "id": self._id(), "artistId": artist["id"], "title": body["title"],
                "foreignAlbumId": body["foreignAlbumId"],
                "monitored": False if (is_new_artist and self.refresh_unmonitors) else body.get("monitored", True),
                "statistics": {"percentOfTracks": 0}})
        return {"id": self._next_id, "artistId": artist["id"], "artist": artist, "title": body["title"],
                "foreignAlbumId": body["foreignAlbumId"]}


# ----------------------------------------------------------------- Jellyfin

class MockJellyfin(_Server):
    """Minimal Jellyfin API: /Users, /Items (Audio + Playlist search), /Playlists.

    items:     [{"Id","Name","Artists":[...],"AlbumArtist","Album"}]
    playlists: {id: {"Id","Name","items":[item ids]}}
    """

    def __init__(self, api_key="jf-key", users=("Daniel",)):
        super().__init__()
        self.api_key = api_key
        self.users = [{"Name": n, "Id": f"user-{i}"} for i, n in enumerate(users)]
        self.items: list[dict] = []
        self.playlists: dict[str, dict] = {}
        self.fail_items = False       # make /Items answer 500
        self.fail_playlist_writes = False   # make POST /Playlists... answer 500
        self.drop_playlist_appends = False  # answer POST /Playlists/{id}/Items with 204 but keep nothing
        self.fail_search_terms: set[str] = set()  # /Items searches for these (lowercase) terms answer 500
        self._n = 0

    def add_item(self, item_id, name, artist, album=None):
        it = {"Id": item_id, "Name": name, "Artists": [artist], "AlbumArtist": artist, "Album": album}
        self.items.append(it)
        return it

    def playlist_by_name(self, name):
        return next((p for p in self.playlists.values() if p["Name"] == name), None)

    def handle(self, method, path, q, body, headers):
        m = re.search(r'Token="([^"]*)"', headers.get("Authorization", ""))
        if not m or m.group(1) != self.api_key:
            return 401, {}
        if method == "GET" and path == "/Users":
            return 200, self.users
        if method == "GET" and path == "/Items":
            if self.fail_items:
                return 500, {"error": "boom"}
            if "ids" in q:  # lookup of known items
                wanted = set(q["ids"].split(","))
                return 200, {"Items": [it for it in self.items if it["Id"] in wanted]}
            term = q.get("searchTerm", "").lower()
            if term in self.fail_search_terms:
                return 500, {"error": "boom"}
            if q.get("includeItemTypes") == "Playlist":
                return 200, {"Items": [{"Id": p["Id"], "Name": p["Name"]} for p in self.playlists.values()
                                       if term in p["Name"].lower()]}
            return 200, {"Items": [it for it in self.items if term in it["Name"].lower()][:int(q.get("limit", 50))]}
        if method == "POST" and self.fail_playlist_writes and path.startswith("/Playlists"):
            return 500, {"error": "boom"}
        if method == "POST" and path == "/Playlists":
            self._n += 1
            pid = f"pl{self._n}"
            self.playlists[pid] = {"Id": pid, "Name": body["Name"], "items": list(body.get("Ids", []))}
            return 200, {"Id": pid}
        m = re.fullmatch(r"/Playlists/([^/]+)/Items", path)
        if m and m.group(1) in self.playlists:
            pl = self.playlists[m.group(1)]
            if method == "GET":  # like Jellyfin 10.9+: the entry id is the item id
                names = {it["Id"]: it["Name"] for it in self.items}
                return 200, {"Items": [{"Id": i, "PlaylistItemId": i, "Name": names.get(i, i)} for i in pl["items"]]}
            if method == "DELETE":  # removes every entry of the given items, like Jellyfin 10.9+
                gone = set(q.get("entryIds", "").split(","))
                pl["items"] = [i for i in pl["items"] if i not in gone]
                return 204, {}
            if method == "POST":
                if not self.drop_playlist_appends:
                    pl["items"] += [i for i in q.get("ids", "").split(",") if i]
                return 204, {}
        return 404, {}


# --------------------------------------------------------------- artwork images

PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


def png_bytes(path: str, size: int = 160) -> bytes:
    """A small generated PNG (diagonal gradient, a circle and a bar); the colours depend on `path`."""
    h = hashlib.sha1(path.encode()).digest()
    hue = h[0] / 255
    c1 = colorsys.hsv_to_rgb(hue, 0.75, 0.95)
    c2 = colorsys.hsv_to_rgb((hue + 0.18 + h[1] / 1000) % 1, 0.85, 0.45)
    c3 = colorsys.hsv_to_rgb((hue + 0.5) % 1, 0.35, 1.0)
    cx, cy, rad = size * (0.3 + h[2] / 800), size * (0.35 + h[3] / 800), size * 0.22
    bar_top = int(size * (0.62 + h[4] / 1500))
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter type: none
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            rgb = [a + (b - a) * t for a, b in zip(c1, c2)]
            if (x - cx) ** 2 + (y - cy) ** 2 < rad ** 2:
                rgb = c3
            elif y >= bar_top and (x // 8) % 2 == 0:
                rgb = [v * 0.55 for v in rgb]
            rows += bytes(int(v * 255) for v in rgb)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


class MockImages:
    """Image server for artwork. Every path gets its own generated PNG (query string ignored);
    /text answers text/plain, /huge an image larger than the 2 MB cap, /missing a 404.

    requests: paths served (with query), for cache-hit assertions.
    """

    def __init__(self):
        self.requests: list[str] = []
        self._png: dict[str, bytes] = {}
        self._httpd = None

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = urlparse(self.path).path
                outer.requests.append(self.path)
                ctype, body, status = "image/png", None, 200
                if path == "/missing":
                    status, ctype, body = 404, "text/plain", b"nope"
                elif path == "/text":
                    ctype, body = "text/plain", b"not an image"
                elif path == "/huge":
                    body = bytes(2 * 1024 * 1024 + 1)
                else:
                    body = outer._png.setdefault(path, png_bytes(path))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.01), daemon=True).start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    @property
    def host(self) -> str:
        return "127.0.0.1"

    def thumbs(self, name: str, sizes=(60, 120)) -> list[dict]:
        """ytmusicapi-style thumbnail list for artwork called `name`."""
        return [{"url": f"{self.url}/{name}?s={w}", "width": w, "height": w} for w in sizes]

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ------------------------------------------------- YouTube Music / MusicBrainz

def yt_track(title, artists, album=None, thumbnails=None, thumb_key="thumbnails", video_id=None):
    """A track dict shaped like ytmusicapi's playlist items (thumb_key="thumbnail" for watch-queue items)."""
    t = {"title": title, "artists": [{"name": a, "id": None} for a in artists],
         "album": {"name": album, "id": "x"} if album else None}
    if video_id:
        t["videoId"] = video_id
    if thumbnails is not None:
        t[thumb_key] = thumbnails
    return t


class FakeYTM:
    """Stand-in for ytmusicapi.YTMusic.

    playlists:      {id: {"title": ..., "tracks": [...], "thumbnails": [...]?} | Exception}
    watch:          {id: [tracks]}  (get_watch_playlist, used for RD... mixes)
    search_results: f(query, filter) -> list of song dicts (default: nothing found)
    """

    def __init__(self, playlists=None, watch=None, search_results=None):
        self.playlists = playlists or {}
        self.watch = watch or {}
        self.search_results = search_results or (lambda q, f: [])
        self.calls: list[tuple] = []

    def get_playlist(self, pid, limit=None):
        self.calls.append(("get_playlist", pid))
        v = self.playlists.get(pid)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise KeyError(f"no such playlist {pid}")
        return v

    def get_watch_playlist(self, playlistId=None, limit=25):
        self.calls.append(("get_watch_playlist", playlistId))
        return {"tracks": self.watch.get(playlistId, [])}

    def search(self, query, filter=None, limit=20):
        self.calls.append(("search", query))
        return self.search_results(query, filter)


def install_fake_ytm(monkeypatch, fake):
    from app import engine
    monkeypatch.setattr(engine, "_make_ytm", lambda settings: fake)
    return fake


class FakeMusicBrainz:
    """Responder for MusicBrainz searches; plugged in by patching MusicBrainz._search so the
    real album()/song() ranking code still runs.

    release_groups: raw "release-groups" entries; recordings: raw "recordings" entries.
    """

    def __init__(self, release_groups=None, recordings=None):
        self.release_groups = release_groups or []
        self.recordings = recordings or []
        self.queries: list[tuple[str, str]] = []

    def search(self, entity, query):
        self.queries.append((entity, query))
        if entity == "release-group":
            return {"release-groups": self.release_groups}
        return {"recordings": self.recordings}


def install_fake_musicbrainz(monkeypatch, fake=None):
    from app import engine
    fake = fake or FakeMusicBrainz()
    monkeypatch.setattr(engine.MusicBrainz, "_search", lambda self, entity, query: fake.search(entity, query))
    return fake
