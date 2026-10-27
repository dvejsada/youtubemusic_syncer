"""Command line: python -m app.engine "<playlist url>" [--dry-run] [--mirror] [--only lidarr|jellyfin]."""
from __future__ import annotations

import argparse
import os
import sys

from app.engine.models import EngineError, Settings
from app.engine.sync import sync_playlist


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
    p.add_argument("--mirror", action="store_true",
                   help="make the Jellyfin playlist match exactly: remove other songs and duplicates, restore the order")
    p.add_argument("--cover", action="store_true",
                   help="set the YouTube cover as the Jellyfin playlist image (the CLI keeps no record, so every "
                        "run sets it again)")
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
        jellyfin_user=args.jellyfin_user, jellyfin_cover=args.cover, ytm_auth_path=args.ytm_auth,
        finalize_timeout_min=args.finalize_timeout,
    )
    try:
        sync_playlist(settings, args.playlist, jellyfin_name=args.name,
                      do_lidarr=args.only != "jellyfin", do_jellyfin=args.only != "lidarr",
                      dry_run=args.dry_run, jellyfin_mirror=args.mirror, log=lambda m="": print(m, flush=True))
    except EngineError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0
