"""Public result and settings types of the engine."""
from __future__ import annotations

from dataclasses import dataclass, field


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
    jellyfin_cover: bool = True  # the YouTube cover becomes the Jellyfin playlist's image
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
    jellyfin_removed: int = 0        # mirror: entries removed from the Jellyfin playlist
    jellyfin_moved: int = 0          # mirror: songs re-added to restore the YouTube order
    jellyfin_lookup_errors: int = 0  # tracks whose Jellyfin search failed (marked "missing" without knowing)
    jellyfin_playlist_id: str = ""
    errors: list[str] = field(default_factory=list)
    status: str = "ok"               # ok | warning (some not found / missing) | error
    stopped: bool = False            # extra: should_stop() aborted the run early
    lidarr_pending: list[str] = field(default_factory=list)  # wait_for_lidarr=False: for lidarr_finalize_step
    cover_url: str = ""              # YouTube Music playlist cover (source URL); "" for mixes
