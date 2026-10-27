"""app.engine - YouTube Music playlist -> Lidarr (albums) + Jellyfin (playlist)

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

Modules: models (result / settings types), matching (normalisation, similarity), cache (lookups remembered
between syncs), clock (time hooks), common (redaction, errors), http (the shared API client), ytm,
musicbrainz, lidarr, jellyfin (one per service), sync (sync_playlist), cli.
Everything callers use is re-exported here.
"""
from app.engine.cache import CACHE_VERSION, RETRY_DAYS, Cache
from app.engine.cli import main, settings_from_env
from app.engine.common import ApiError, _fatal, _redactor
from app.engine.jellyfin import Jellyfin, PlaylistUpdate, check_jellyfin
from app.engine.lidarr import (FinalizeStep, Lidarr, check_lidarr, lidarr_base_url,
                               lidarr_finalize_step, lidarr_options, resolve)
from app.engine.matching import (DASH_SUFFIX, EDITION, EMPTY_PARENS, JUNK_WORDS, NOISE,
                                 PIPE_SUFFIX, clean, norm, sim, split_title)
from app.engine.models import EngineError, Settings, SyncResult, TrackResult
from app.engine.musicbrainz import MusicBrainz
from app.engine.sync import sync_playlist
from app.engine.ytm import (check_ytm, fetch_raw, parse_playlist_ref, pick_cover, pick_thumb,
                            read_playlist, read_playlist_full, take_cover)

__all__ = [
    "ApiError",
    "CACHE_VERSION",
    "Cache",
    "DASH_SUFFIX",
    "EDITION",
    "EMPTY_PARENS",
    "EngineError",
    "FinalizeStep",
    "JUNK_WORDS",
    "Jellyfin",
    "Lidarr",
    "MusicBrainz",
    "NOISE",
    "PIPE_SUFFIX",
    "PlaylistUpdate",
    "RETRY_DAYS",
    "Settings",
    "SyncResult",
    "TrackResult",
    "_fatal",
    "_redactor",
    "check_jellyfin",
    "check_lidarr",
    "check_ytm",
    "clean",
    "fetch_raw",
    "lidarr_base_url",
    "lidarr_finalize_step",
    "lidarr_options",
    "main",
    "norm",
    "parse_playlist_ref",
    "pick_cover",
    "pick_thumb",
    "read_playlist",
    "read_playlist_full",
    "resolve",
    "settings_from_env",
    "sim",
    "split_title",
    "sync_playlist",
    "take_cover",
]
