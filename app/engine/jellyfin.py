"""Jellyfin: matching tracks against the library and maintaining the playlist (append-only or mirror)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.cache import Cache
from app.engine.common import _CHECK_TIMEOUT, _NOLOG, ApiError, EngineError, _base_url, _fatal, _redactor
from app.engine.http import Http
from app.engine.matching import DASH_SUFFIX, NOISE, norm, sim
from app.engine.models import Settings, TrackResult


@dataclass
class PlaylistUpdate:
    """What Jellyfin.ensure_playlist did (or, in dry-run, would do)."""
    message: str
    playlist_id: str
    new: list[str]                                     # items added that weren't in the playlist before
    lost: list[str] = field(default_factory=list)      # wanted items not in the playlist when read back
    removed: list[str] = field(default_factory=list)   # mirror: names of songs not in the YouTube playlist
    duplicates: int = 0                                # mirror: extra copies of a song that were removed
    moved: int = 0                                     # mirror: songs re-added to restore the order
    written: bool = False
    exact: bool = True                                 # read back: the playlist holds exactly the wanted ids


def _auth_header(key: str) -> dict:
    return {"Authorization": f'MediaBrowser Token="{key}"'}


class Jellyfin:
    CONNECT_TIMEOUT = 10

    def __init__(self, url, key, user, dry=False, log=_NOLOG):
        self.url = _base_url(url)
        # only "busy" answers are retried: a 500 for one search is usually that search, not a busy server
        self.http = Http(self.url, _auth_header(key), timeout=(self.CONNECT_TIMEOUT, 60), retry_on=(429, 502, 503, 504))
        self.dry = dry
        users = self.req("GET", "/Users")
        match = [u for u in users if not user or u["Name"].lower() == user.lower()]
        if not match:
            raise EngineError(f"Jellyfin user '{user}' not found. Users: {[u['Name'] for u in users]}")
        self.uid = match[0]["Id"]
        self.user_name = match[0]["Name"]
        log(f"Jellyfin: user={match[0]['Name']}")

    def req(self, method, path, params=None, json=None):
        """Every Jellyfin call goes through here (reads are retried when Jellyfin is busy)."""
        if method == "GET":
            return self.http.get(path, params)
        return self.http.send(method, path, json=json, params=params)

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

    def existing_ids(self, ids: list[str]) -> set:
        """Which of these library items still exist for the user."""
        out = set()
        for k in range(0, len(ids), 100):
            out |= {i["Id"] for i in self.req("GET", "/Items", params={
                "userId": self.uid, "ids": ",".join(ids[k:k + 100]), "recursive": "true",
            }).get("Items", [])}
        return out

    def playlist_entries(self, playlist_id) -> list[dict]:
        """The playlist in order: [{"entry", "id", "name"}]. Current Jellyfin uses the item id as entry id."""
        items = self.req("GET", f"/Playlists/{playlist_id}/Items", params={"userId": self.uid}).get("Items", [])
        return [{"entry": i.get("PlaylistItemId") or i["Id"], "id": i["Id"], "name": i.get("Name") or i["Id"]}
                for i in items]

    def _append(self, playlist_id, ids):
        for k in range(0, len(ids), 50):
            self.req("POST", f"/Playlists/{playlist_id}/Items",
                     params={"ids": ",".join(ids[k:k + 50]), "userId": self.uid})

    def _saved_playlist(self, playlist_id) -> list[dict] | None:
        """Entries of the playlist saved by an earlier sync, or None if it is gone (deleted in Jellyfin, or
        not visible to the configured user any more)."""
        try:
            return self.playlist_entries(playlist_id)
        except ApiError as e:
            if e.status in (400, 403, 404):
                return None
            raise

    def ensure_playlist(self, name, ids, mirror=False, playlist_id="") -> "PlaylistUpdate":
        """Append-only, or with `mirror` make an existing playlist hold exactly `ids` in this order.
        The playlist saved by an earlier sync (`playlist_id`) is used even if it was renamed in Jellyfin;
        only when it is gone is the playlist looked up by `name` (and created if there is none).
        After writing, the playlist is read back: ids that are not in it end up in `lost`."""
        before = self._saved_playlist(playlist_id) if playlist_id else None
        if before is not None:
            pl, label = {"Id": playlist_id}, f"id {playlist_id}"
        else:
            pls = self.req("GET", "/Items", params={
                "userId": self.uid, "includeItemTypes": "Playlist", "recursive": "true", "searchTerm": name,
            }).get("Items", [])
            pl, label = next((p for p in pls if p["Name"] == name), None), f"'{name}'"
            if pl:
                before = self.playlist_entries(pl["Id"])
        if not pl:
            if self.dry:
                return PlaylistUpdate(f"would create playlist '{name}' with {len(ids)} tracks", "", list(ids))
            res = self.req("POST", "/Playlists", json={
                "Name": name, "Ids": ids, "UserId": self.uid, "MediaType": "Audio",
            })
            upd = PlaylistUpdate(f"created playlist '{name}' with {len(ids)} tracks (id {res.get('Id')})",
                                 res.get("Id") or "", list(ids), written=True)
        else:
            have = {e["id"] for e in before}
            upd = PlaylistUpdate("", pl["Id"], [i for i in ids if i not in have])
            verb = "would add" if self.dry else "added"
            upd.message = f"playlist {label} exists: {verb} {len(upd.new)} new track(s)"
            if mirror:
                self._mirror(upd, before, ids)
            elif upd.new and not self.dry:
                self._append(upd.playlist_id, upd.new)
                upd.written = True
        if upd.written and upd.playlist_id:
            # Jellyfin can answer a write with success and still not keep the items: check
            after = [e["id"] for e in self.playlist_entries(upd.playlist_id)]
            upd.lost = [i for i in ids if i not in set(after)]
            upd.new = [i for i in upd.new if i in set(after)]
            upd.exact = after == list(ids)
        return upd

    def _mirror(self, upd: "PlaylistUpdate", before: list[dict], ids: list[str]) -> None:
        """Jellyfin doesn't let an API key move or replace playlist items, and removing an entry removes every
        copy of that song. So: remove the songs that don't belong, keep the longest start that is already in
        the right order, remove everything after it and append the rest of `ids` in order."""
        wanted, seen = set(ids), set()
        for e in before:
            if e["id"] not in wanted:
                upd.removed.append(e["name"])
            elif e["id"] in seen:
                upd.duplicates += 1
            seen.add(e["id"])
        rest = [e for e in before if e["id"] in wanted]
        k = 0
        while k < min(len(rest), len(ids)) and rest[k]["id"] == ids[k]:
            k += 1
        later = {e["id"] for e in rest[k:]}
        # a kept song must not come again later: removing that copy would remove the kept one too
        k = next((j for j in range(k) if rest[j]["id"] in later), k)
        drop = list(dict.fromkeys([e["entry"] for e in before if e["id"] not in wanted]
                                  + [e["entry"] for e in rest[k:]]))
        add = ids[k:]
        upd.moved = sum(i in seen for i in add)
        if drop or add != upd.new:
            upd.message += (f"; mirror: {'would remove' if self.dry else 'removed'} {len(upd.removed)} song(s) "
                            f"not in the YouTube playlist and {upd.duplicates} duplicate entr"
                            f"{'y' if upd.duplicates == 1 else 'ies'}, re-added {upd.moved} to restore the order")
        if self.dry:
            return
        for j in range(0, len(drop), 50):
            self.req("DELETE", f"/Playlists/{upd.playlist_id}/Items", params={"entryIds": ",".join(drop[j:j + 50])})
        self._append(upd.playlist_id, add)
        upd.written = bool(drop or add)


def _sync_jellyfin(jf: Jellyfin, name: str, tracks: list[dict], results: list[TrackResult],
                   log, redact, should_stop, errors: list[str], mirror: bool = False,
                   saved_id: str = "", cache: Cache | None = None) -> tuple[str, dict]:
    """Runs the Jellyfin phase. Returns (playlist id or '', mirror counts {"removed", "moved"}).
    `saved_id` is the Jellyfin playlist an earlier sync used. A match of an earlier sync (`cache`) is reused
    while its library item exists, the track is unchanged and the match is younger than RETRY_DAYS; the
    other tracks are searched (an older match is kept if the search finds nothing)."""
    stats: dict = {}
    cache = cache if cache is not None else Cache()
    lookup_failed = False
    log("")
    log(f"== Jellyfin: matching {len(tracks)} tracks ==")
    # what the match was made for: a changed title / artist / album (e.g. corrected on YouTube) is searched again
    sigs = [f"{norm((t['artists'] or [''])[0])}|{norm(t['title'])}|{norm(t['album'] or '')}" for t in tracks]
    keys = [f"{jf.url}|{jf.uid}|" + (t.get("video_id") or sig) for t, sig in zip(tracks, sigs)]
    cache.prefetch("jellyfin", keys)
    remembered = {k: c for k, sig in zip(keys, sigs) if (c := cache.get("jellyfin", k)) and c.get("sig") == sig}
    try:
        alive = jf.existing_ids(sorted({c["id"] for c in remembered.values()})) if remembered else set()
    except Exception as e:
        log(f"  !  could not check the matches of earlier syncs, searching again: {redact(e)}")
        alive = set()
    reused = 0
    ids, missing = [], []
    matched: list[tuple[TrackResult, str]] = []
    duplicates: list[TrackResult] = []
    first_pos: dict[str, int] = {}  # Jellyfin item id -> position of the first track matched to it
    consecutive_errors = 0
    for i, (t, tr) in enumerate(zip(tracks, results)):
        if should_stop():
            log("Stopped.")
            return "", stats
        try:
            known = remembered.get(keys[i])
            known = known if known and known["id"] in alive else None
            if known and Cache.recent(known):
                it = {"Id": known["id"]}
                reused += 1
            else:
                it = jf.find_track(t)
                if it:
                    cache.put("jellyfin", keys[i], {"id": it["Id"], "sig": sigs[i]})
                elif known:  # a re-check found nothing: the earlier match still stands
                    it = {"Id": known["id"]}
            consecutive_errors = 0
        except Exception as e:
            log(f"  !  {t['title']}: {redact(e)}")
            lookup_failed = True
            consecutive_errors += 1
            if consecutive_errors >= 5:
                errors.append("Jellyfin: stopped after repeated errors: " + redact(e))
                log("  Jellyfin keeps failing, giving up on the Jellyfin step.")
                for r in results[i:]:
                    r.jellyfin_status = "skipped"
                return "", stats
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
    log(f"  matched {len(ids)}/{len(tracks)}" + (f" ({reused} remembered from earlier syncs)" if reused else ""))
    if duplicates:
        log(f"  {len(duplicates)} track(s) are the same Jellyfin song as an earlier track (added once):")
        for tr in duplicates:
            log(f"    = #{tr.position} {', '.join(tr.artists)} - {tr.title}  ({tr.jellyfin_note})")
    if mirror and lookup_failed:
        mirror = False
        log("  Mirror skipped this time: some Jellyfin lookups failed, so nothing is removed or reordered.")
    elif mirror and not ids:
        log("  Mirror skipped: no track matched, the Jellyfin playlist is left as it is.")
    playlist_id = ""
    if ids:
        try:
            upd = jf.ensure_playlist(name, ids, mirror=mirror, playlist_id=saved_id)
            playlist_id = upd.playlist_id
            log("  " + upd.message)
        except Exception as e:
            errors.append("Jellyfin playlist update failed: " + redact(e))
            log(f"  !  playlist update failed: {redact(e)}")
            for tr in [tr for tr, _ in matched] + duplicates:
                tr.jellyfin_status = "skipped"
            return "", stats
        stats.update(removed=len(upd.removed) + upd.duplicates, moved=upd.moved)
        for song in upd.removed:
            log(f"    - removed '{song}' (not in the YouTube playlist)")
        new, lost = set(upd.new), set(upd.lost)
        for tr, item_id in matched:
            tr.jellyfin_status = "not_added" if item_id in lost else "added" if item_id in new else "in_playlist"
        if lost:
            errors.append(f"Jellyfin accepted {len(lost)} track(s) for playlist '{name}' but did not keep them")
            log(f"  !  {len(lost)} track(s) are not in the playlist after adding them:")
            for tr, item_id in matched:
                if item_id in lost:
                    log(f"    - #{tr.position} {', '.join(tr.artists)} - {tr.title}")
        elif mirror and not upd.exact and not jf.dry:
            errors.append(f"Jellyfin did not apply all mirror changes to playlist '{name}'")
            log("  !  the playlist still differs from the YouTube playlist after mirroring")
    if missing:
        log(f"  not in library yet ({len(missing)}) - will be appended on a later sync once Lidarr has them:")
        for t in missing:
            log(f"    - {', '.join(t['artists'])} - {t['title']}" + (f"  [{t['album']}]" if t['album'] else ""))
    return playlist_id, stats


def check_jellyfin(settings: Settings) -> tuple[bool, str]:
    if not (settings.jellyfin_url and settings.jellyfin_api_key):
        return False, "Not configured"
    redact = _redactor(settings)
    try:
        users = Http(_base_url(settings.jellyfin_url), _auth_header(settings.jellyfin_api_key),
                     timeout=_CHECK_TIMEOUT, attempts=1).get("/Users")
        user = settings.jellyfin_user
        match = [u for u in users if not user or u["Name"].lower() == user.lower()]
        if not match:
            return False, f"user '{user}' not found. Users: {[u['Name'] for u in users]}"
        return True, f"Connected · user {match[0]['Name']}"
    except Exception as e:
        return False, str(_fatal("Jellyfin", e, settings.jellyfin_url, redact)).removeprefix("Jellyfin: ")
