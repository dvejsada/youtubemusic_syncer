"""Lidarr: finding the release of a track (with the MusicBrainz fallback), adding / monitoring albums."""
from __future__ import annotations

from app.engine import clock
from app.engine.cache import RETRY_DAYS, Cache
from app.engine.common import _CHECK_TIMEOUT, _NOLOG, EngineError, _base_url, _fatal, _redactor
from app.engine.http import Http
from app.engine.matching import clean, norm, sim
from app.engine.models import Settings, TrackResult
from app.engine.musicbrainz import MusicBrainz
from dataclasses import dataclass, field


class Lidarr:
    CONNECT_TIMEOUT = 10

    BUSY_COMMANDS = {"RefreshArtist", "BulkRefreshArtist", "RefreshAlbum", "AddArtist"}

    def __init__(self, url, key, root=None, quality=None, metadata=None, dry=False, log=_NOLOG, connect_only=False):
        """connect_only: just the API session (for lidarr_finalize_step), no profiles / artist list."""
        self.base = _base_url(url)  # identifies this Lidarr (lookup cache keys, pending albums)
        self.url = self.base + "/api/v1"
        # Lidarr's metadata server (api.lidarr.audio) often returns 503 under load: reads are retried
        self.http = Http(self.url, {"X-Api-Key": key}, timeout=(self.CONNECT_TIMEOUT, 90))
        self.dry = dry
        self.log = log
        self._library = self._artist_by_id = None
        self.pending = set()
        if connect_only:
            return
        self.root = self._pick("rootfolder", root, "path")["path"]
        self.qp = self._pick("qualityprofile", quality, "name")["id"]
        mp = self._pick("metadataprofile", metadata, "name")
        self.mp = mp["id"]
        self.artists = {a["foreignArtistId"]: a for a in self.get("artist")}
        self._album_cache = {}
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
        return self.http.get(path, params)

    def send(self, method, path, body):
        if self.dry:
            return {}
        return self.http.send(method, path, json=body, timeout=(self.CONNECT_TIMEOUT, 120))

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
            clock.sleep(0.3)
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
            return self.ensure_existing(artist, alb)

        return self._add_album(found)

    def ensure_existing(self, artist, alb):
        """The album is in Lidarr already: make sure it and its artist are monitored and it is searched."""
        if alb.get("monitored") and (alb.get("statistics") or {}).get("percentOfTracks", 0) >= 100:
            return "already downloaded"
        if not alb.get("monitored"):
            self.send("PUT", "album/monitor", {"albumIds": [alb["id"]], "monitored": True})
        if not artist.get("monitored"):
            artist["monitored"] = True
            self.send("PUT", f"artist/{artist['id']}", artist)
        self.send("POST", "command", {"name": "AlbumSearch", "albumIds": [alb["id"]]})
        return "monitored + search started"

    def library_album(self, foreign_album_id):
        """(artist, album) of an album that is in Lidarr's library, or None. One request for the whole run."""
        if self._library is None:
            self._library = {a["foreignAlbumId"]: a for a in self.get("album")}
            self._artist_by_id = {a["id"]: a for a in self.artists.values()}
        alb = self._library.get(foreign_album_id)
        artist = alb and self._artist_by_id.get(alb.get("artistId"))
        return (artist, alb) if artist else None

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
        deadline = clock.now() + timeout_min * 60
        clock.sleep_interruptible(10, should_stop)
        while clock.now() < deadline and not should_stop():
            busy = self.refreshing()
            if not busy:
                break
            log(f"  ...{busy} refresh task(s) still running")
            clock.sleep_interruptible(15, should_stop)
        if should_stop():
            log("Stopped while waiting for Lidarr; albums will be picked up on the next sync.")
            return None
        done, missing, unmonitored = self.monitor_and_search(self.pending)
        log(f"Lidarr: {len(done)} album(s) monitored and searched ({unmonitored} had been unmonitored by Lidarr).")
        if missing:
            log(f"  {len(missing)} album(s) not visible in Lidarr yet - they will be picked up on the next sync.")
        return set(missing)

    def refreshing(self) -> int:
        """Number of artist refresh / import tasks Lidarr is still running."""
        return sum(c.get("name") in self.BUSY_COMMANDS and c.get("status") in ("queued", "started")
                   for c in self.get("command"))

    def monitor_and_search(self, foreign_album_ids) -> tuple[list, list, int]:
        """Monitor the albums (Lidarr's refresh of a new artist unmonitors them) and search for them.
        Returns (done, missing, unmonitored): the foreign ids that were handled / are not visible in
        Lidarr yet, and how many albums had been unmonitored."""
        by_foreign = {}  # one small request per album, not the whole (possibly large) library
        for f in dict.fromkeys(foreign_album_ids):
            by_foreign.update({a["foreignAlbumId"]: a for a in self.get("album", foreignAlbumId=f)
                               if a.get("foreignAlbumId") == f})
        done = [f for f in foreign_album_ids if f in by_foreign]
        missing = [f for f in foreign_album_ids if f not in by_foreign]
        unmonitored = [by_foreign[f]["id"] for f in done if not by_foreign[f].get("monitored")]
        if unmonitored:
            self.send("PUT", "album/monitor", {"albumIds": unmonitored, "monitored": True})
        ids = [by_foreign[f]["id"] for f in done]
        for k in range(0, len(ids), 50):
            self.send("POST", "command", {"name": "AlbumSearch", "albumIds": ids[k:k + 50]})
        return done, missing, len(unmonitored)


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
                 log, redact, timeout_min: int, should_stop, cache: Cache | None = None,
                 wait: bool = True) -> tuple[bool, list[str]]:
    """Runs the Lidarr phase and fills the TrackResults. Returns (stopped early, pending albums).
    Releases found before (`cache`) are not looked up again while they are in Lidarr's library.
    Without `wait`, albums of newly added artists are not monitored here but returned as pending
    for lidarr_finalize_step (the app's worker does that in the background)."""
    cache = cache if cache is not None else Cache()
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
    reused = 0
    cache.prefetch("lidarr", ["|".join((lid.base, *key)) for key in albums])
    for key, a in albums.items():
        if should_stop():
            log("Stopped.")
            return True, []
        artists, album = a["artists"], a["album"]
        label = f"{artists[0]} - {album}" if album else f"{artists[0]} - {a['titles'][0]} (no album on YouTube)"
        ckey = "|".join((lid.base, *key))
        try:
            cached = cache.get("lidarr", ckey)
            if cached and not cached["fid"] and Cache.recent(cached):
                failed.append(label)
                log(f"  ?  {label}  - not on MusicBrainz, add manually (looked up before; tried again after "
                    f"{RETRY_DAYS} days)")
                mark(a, "not_found", "", "not on MusicBrainz, add manually")
                continue
            found = in_library = None
            if cached and cached["fid"]:
                # found before: no lookup while the album is in Lidarr (else the exact id lookup adds it again)
                in_library = lid.library_album(cached["fid"])
                if not in_library:
                    try:
                        found = lid.by_mbid(cached["fid"])
                    except Exception:
                        found = None  # look it up from scratch below
            if in_library or found:
                fid, release, how = cached["fid"], cached["release"], cached["how"]
                reused += 1
            else:
                found, how, err = resolve(lid, mb, a)
                if not found:
                    failed.append(label)
                    if err:
                        log(f"  ?  {label}  - lookup failed: {redact(err)}")
                        mark(a, "error", "", f"lookup failed: {redact(err)}")
                    else:
                        log(f"  ?  {label}  - not on MusicBrainz, add manually")
                        mark(a, "not_found", "", "not on MusicBrainz, add manually")
                        cache.put("lidarr", ckey, {"fid": ""})
                    continue
                kind = found.get("albumType") or "?"
                fid, release = found["foreignAlbumId"], f"{found['artist']['artistName']} - {found['title']} [{kind}]"
                cache.put("lidarr", ckey, {"fid": fid, "release": release, "how": how})
            how_note = how.strip().strip("()")
            if fid in done:
                log(f"  =  {label}  -> same release as above, skipped")
                mark(a, "duplicate", release, _join_note(how_note, "same release as another track"))
                by_release.setdefault(fid, []).append(a)
                continue
            done.add(fid)
            status = lid.ensure_existing(*in_library) if in_library else lid.ensure(found)
            log(f"  ✓  {label}  ->  {release}{how}: {tag}{status}")
            st, extra = _ENSURE_STATUS.get(status, ("added", status))
            mark(a, st, release, _join_note(how_note, extra))
            by_release.setdefault(fid, []).append(a)
        except Exception as e:
            failed.append(label)
            log(f"  !  {label}: {redact(e)}")
            mark(a, "error", "", redact(e))

    if reused:
        log(f"  ({reused} of them found by an earlier sync, not looked up again)")
    if failed:
        log("")
        log(f"  {len(failed)} item(s) need manual adding in Lidarr (see '?' / '!' above).")
    if not wait:
        pending = [] if dry else sorted(lid.pending)
        if pending:
            log("")
            log(f"Lidarr: {len(pending)} album(s) of new artists will be monitored and searched once Lidarr "
                "has imported the artists (checked in the background).")
        return bool(should_stop()), pending
    missing = lid.finalize(timeout_min, should_stop)
    if missing:
        for fid in missing:
            for a in by_release.get(fid, []):
                for tr in a["results"]:
                    tr.lidarr_note = _join_note(tr.lidarr_note, "not visible in Lidarr yet, next sync will finish it")
    return bool(should_stop()), []  # stopped: the finalize wait was interrupted




@dataclass
class FinalizeStep:
    busy: bool = False                                 # Lidarr is still importing: nothing was done
    done: list[str] = field(default_factory=list)      # albums monitored and searched
    missing: list[str] = field(default_factory=list)   # not visible in Lidarr (yet)
    unmonitored: int = 0                               # of `done`, unmonitored by Lidarr's artist refresh


def lidarr_base_url(settings: Settings) -> str:
    """Identifies the configured Lidarr (what pending albums belong to), like Lidarr.base."""
    return _base_url(settings.lidarr_url)


def lidarr_finalize_step(settings: Settings, foreign_album_ids: list[str], force: bool = False) -> FinalizeStep:
    """The non-blocking version of Lidarr.finalize for sync_playlist(wait_for_lidarr=False): while Lidarr is
    still refreshing / importing artists it does nothing (busy, unless `force`); otherwise it monitors and
    searches the albums that are visible. A handful of requests, meant to be repeated until done."""
    lid = Lidarr(settings.lidarr_url, settings.lidarr_api_key, connect_only=True)
    if not force and lid.refreshing():
        return FinalizeStep(busy=True)
    done, missing, unmonitored = lid.monitor_and_search(list(foreign_album_ids))
    return FinalizeStep(done=done, missing=missing, unmonitored=unmonitored)




def _lidarr_http(settings: Settings) -> Http:
    """For the connection checks: short timeouts, no retries."""
    return Http(_base_url(settings.lidarr_url) + "/api/v1", {"X-Api-Key": settings.lidarr_api_key},
                timeout=_CHECK_TIMEOUT, attempts=1)


def check_lidarr(settings: Settings) -> tuple[bool, str]:
    if not (settings.lidarr_url and settings.lidarr_api_key):
        return False, "Not configured"
    redact = _redactor(settings)
    try:
        http = _lidarr_http(settings)
        version = http.get("system/status").get("version", "?")
        n = len(http.get("artist"))
        return True, f"Connected · v{version} · {n} artist{'s' if n != 1 else ''}"
    except Exception as e:
        return False, str(_fatal("Lidarr", e, settings.lidarr_url, redact)).removeprefix("Lidarr: ")


def lidarr_options(settings: Settings) -> dict:
    """Names to offer in the settings form. Empty lists if Lidarr is not configured / reachable."""
    out = {"root": [], "quality": [], "metadata": []}
    if not (settings.lidarr_url and settings.lidarr_api_key):
        return out
    try:
        http = _lidarr_http(settings)
        for key, endpoint, fld in (("root", "rootfolder", "path"), ("quality", "qualityprofile", "name"),
                                   ("metadata", "metadataprofile", "name")):
            out[key] = [str(i[fld]) for i in http.get(endpoint) if i.get(fld) is not None]
    except Exception:
        return {"root": [], "quality": [], "metadata": []}
    return out
