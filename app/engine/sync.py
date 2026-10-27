"""sync_playlist: one YouTube Music playlist -> Lidarr (albums) + Jellyfin (playlist)."""
from __future__ import annotations

from app.engine.cache import Cache
from app.engine.common import _fatal, _redactor
from app.engine.jellyfin import Jellyfin, _sync_jellyfin
from app.engine.lidarr import Lidarr, _sync_lidarr
from app.engine.models import EngineError, Settings, SyncResult, TrackResult
from app.engine.ytm import read_playlist_full


def sync_playlist(settings: Settings, ref: str, *, jellyfin_name: str | None = None,
                  do_lidarr: bool = True, do_jellyfin: bool = True, dry_run: bool = False,
                  jellyfin_mirror: bool = False, jellyfin_playlist_id: str = "",
                  cache: Cache | None = None, wait_for_lidarr: bool = True,
                  log=print, should_stop=lambda: False) -> SyncResult:
    """Sync one YouTube Music playlist. Raises EngineError for fatal problems (service
    unreachable / unauthorised, playlist unreadable); per-track problems end up in the result.
    With `jellyfin_mirror` the Jellyfin playlist is made to match: other songs and duplicate entries are
    removed and the YouTube order is restored (instead of only appending).
    `jellyfin_playlist_id` (from an earlier result) keeps using that playlist even if it was renamed in Jellyfin.
    `cache` remembers lookups between syncs (see Cache); pass the same one again to only look up what is new.
    Without `wait_for_lidarr` the sync doesn't wait for Lidarr to import new artists: their albums end up in
    `result.lidarr_pending` and lidarr_finalize_step monitors them later."""
    redact = _redactor(settings)
    cache = cache if cache is not None else Cache()

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

    title, is_mix, tracks, cover = read_playlist_full(settings, ref, say, cache)
    say(f"YouTube Music playlist '{title}': {len(tracks)} tracks")

    results = [TrackResult(position=i, title=t["title"], artists=list(t["artists"]), album=t["album"],
                           thumb_url=t.get("thumb") or "")
               for i, t in enumerate(tracks, 1)]
    res = SyncResult(playlist_name=title, is_mix=is_mix, tracks=results, cover_url=cover)

    stopped = False
    if do_lidarr:
        stopped, res.lidarr_pending = _sync_lidarr(lid, tracks, results, dry_run, say, redact,
                                                   settings.finalize_timeout_min, should_stop, cache, wait_for_lidarr)
    else:
        for tr in results:
            tr.lidarr_status = "skipped"
    if do_jellyfin and not stopped:
        res.jellyfin_playlist_id, jf_stats = _sync_jellyfin(jf, jellyfin_name or title, tracks, results, say,
                                                            redact, should_stop, res.errors, jellyfin_mirror,
                                                            jellyfin_playlist_id, cache)
        res.jellyfin_removed, res.jellyfin_moved = jf_stats.get("removed", 0), jf_stats.get("moved", 0)
        res.jellyfin_lookup_errors = jf_stats.get("lookup_errors", 0)
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
