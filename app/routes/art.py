"""Artwork proxy: GET /art/playlist/{pid} and GET /art/track/{tid}.

The browser never talks to YouTube / Google image hosts. The stored source URL is fetched once on the
server (https, allow-listed hosts only, size-capped, image content only), cached on disk under
DATA_DIR/art/<sha1(url)>.<ext> and served with a long private cache lifetime. Any miss or failure is a
plain 404, so the UI keeps showing its gradient tile.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fasthtml.common import APIRouter
from starlette.responses import FileResponse, Response

from app import db
from app.engine.http import YOUTUBE_IMAGE_HOSTS, fetch_image, url_allowed

ar = APIRouter()

# Module-level so tests and the demo can patch them (host suffixes match "host" and "*.host").
ALLOWED_HOSTS: tuple[str, ...] = YOUTUBE_IMAGE_HOSTS
ALLOWED_SCHEMES: tuple[str, ...] = ("https",)

MAX_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = (5, 10)
COVER_PX, THUMB_PX = 544, 120
CACHE_CONTROL = "private, max-age=604800"
NEGATIVE_TTL = 300.0  # seconds a failed URL is not retried

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}
_MEDIA = {ext: mt for mt, ext in _EXT.items()}
_SIZE_SUFFIX = re.compile(r"=w\d+-h\d+|=s\d+")
_SIZED_HOSTS = ("googleusercontent.com", "ggpht.com")

_failed: dict[str, float] = {}  # url -> monotonic time of the last failure
_failed_lock = threading.Lock()


def is_allowed(url: str) -> bool:
    return url_allowed(url, ALLOWED_HOSTS, ALLOWED_SCHEMES)


def resize(url: str, px: int) -> str:
    """Ask Google's image servers for a `px` square where the URL carries a size suffix (=w60-h60-..., =s90)."""
    host = urlsplit(url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in _SIZED_HOSTS):
        return url
    return _SIZE_SUFFIX.sub(lambda m: f"=w{px}-h{px}" if m.group().startswith("=w") else f"=s{px}", url, count=1)


def _art_dir() -> Path:
    d = db.data_dir() / "art"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cached(url: str) -> Path | None:
    key = hashlib.sha1(url.encode()).hexdigest()
    return next((_art_dir() / f"{key}.{ext}" for ext in _MEDIA if (_art_dir() / f"{key}.{ext}").exists()), None)


def _recently_failed(url: str) -> bool:
    with _failed_lock:
        t = _failed.get(url)
        if t is not None and time.monotonic() - t < NEGATIVE_TTL:
            return True
        _failed.pop(url, None)
        return False


def _download(url: str) -> Path | None:
    """Fetch `url` into the cache (temp file + atomic rename, so concurrent first requests are safe)."""
    key = hashlib.sha1(url.encode()).hexdigest()
    tmp = _art_dir() / f"{key}.{uuid.uuid4().hex}.tmp"
    try:
        # fetch_image follows no redirects: a 3xx could lead off the allow-list
        got = fetch_image(url, max_bytes=MAX_BYTES, timeout=FETCH_TIMEOUT, types=tuple(_EXT))
        if got is None:
            return None
        body, ctype = got
        with open(tmp, "wb") as f:
            f.write(body)
        final = _art_dir() / f"{key}.{_EXT[ctype]}"
        os.replace(tmp, final)
        return final
    except (requests.RequestException, OSError):
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _serve(url: str | None, px: int) -> Response:
    if not url or not is_allowed(url):
        return Response(status_code=404)
    url = resize(url, px)
    path = _cached(url)
    if path is None:
        if _recently_failed(url):
            return Response(status_code=404)
        path = _download(url)
        if path is None:
            with _failed_lock:
                _failed[url] = time.monotonic()
            return Response(status_code=404)
    return FileResponse(path, media_type=_MEDIA[path.suffix[1:]],
                        headers={"Cache-Control": CACHE_CONTROL, "X-Content-Type-Options": "nosniff"})


@ar.get("/art/playlist/{pid}")
def playlist_art(pid: int):
    pl = db.get_playlist(pid)
    return _serve(pl and pl.get("cover_url"), COVER_PX)


@ar.get("/art/track/{tid}")
def track_art(tid: int):
    t = db.get_track(tid)
    return _serve(t and t.get("thumb_url"), THUMB_PX)
