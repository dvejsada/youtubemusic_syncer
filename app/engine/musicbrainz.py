"""Direct MusicBrainz search, used when Lidarr's own search fails or misses."""
from __future__ import annotations

import re
import threading

from app.engine import clock
from app.engine.http import Http
from app.engine.matching import clean, sim


_MB_LOCK = threading.Lock()
_MB_LAST = 0.0  # module-global rate limiter: MusicBrainz allows 1 request/second per client


def _mb_throttle() -> None:
    global _MB_LAST
    with _MB_LOCK:
        wait = 1.2 - (clock.now() - _MB_LAST)
        if wait > 0:
            clock.sleep(wait)
        _MB_LAST = clock.now()


class MusicBrainz:
    """Direct MusicBrainz search, used when Lidarr's own search fails or misses."""
    URL = "https://musicbrainz.org/ws/2"
    SKIP_TYPES = {"Compilation", "DJ-mix", "Live", "Mixtape/Street", "Remix", "Interview", "Spokenword"}

    def __init__(self):
        # MusicBrainz answers 503 when a client is too fast: retried after 5, 10, 20, 40 s
        self.http = Http(self.URL, {"User-Agent": "ytm_sync/1.0 (self-hosted YouTube Music -> Lidarr helper)"},
                         timeout=(10, 30), retry_on=(503,), attempts=5, backoff=5, before=_mb_throttle)

    @staticmethod
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _search(self, entity, query):
        return self.http.get(entity, {"query": query, "fmt": "json", "limit": 25})

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
