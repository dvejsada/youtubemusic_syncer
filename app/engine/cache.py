"""Lookups remembered between syncs (see Cache)."""
from __future__ import annotations

from app.engine import clock


CACHE_VERSION = 1   # bump when matching changes, so that earlier lookups are redone
RETRY_DAYS = 7      # a lookup that found nothing, or a Jellyfin match, is looked up again after this many days


class Cache:
    """Lookups remembered between syncs, {(kind, key): value}, so a sync only looks up what is new or
    unresolved. kinds: "ytm" (song version of a YouTube video), "lidarr" (release of an album group),
    "jellyfin" (library item of a track). The app passes a `loader(kind, keys) -> {key: value}` that reads
    the entries a phase needs (see prefetch) and saves `changed` afterwards."""

    def __init__(self, data: dict | None = None, loader=None):
        self.data = data if data is not None else {}
        self.loader = loader
        self.changed: set = set()

    def prefetch(self, kind: str, keys) -> None:
        """Load the remembered entries a phase is about to use, in one batch."""
        missing = [k for k in dict.fromkeys(keys) if k and (kind, k) not in self.data]
        if not (self.loader and missing):
            return
        try:
            loaded = self.loader(kind, missing)
        except Exception:
            return  # nothing remembered: everything is looked up as usual
        for k, v in loaded.items():
            self.data[(kind, k)] = v

    def get(self, kind: str, key: str) -> dict | None:
        v = self.data.get((kind, key))
        return v if isinstance(v, dict) and v.get("v") == CACHE_VERSION else None

    def put(self, kind: str, key: str, value: dict) -> None:
        self.data[(kind, key)] = {**value, "v": CACHE_VERSION, "at": clock.now()}
        self.changed.add((kind, key))

    @staticmethod
    def recent(value: dict) -> bool:
        """Younger than RETRY_DAYS: too recent to look up again."""
        return clock.now() - value.get("at", 0) < RETRY_DAYS * 86400
