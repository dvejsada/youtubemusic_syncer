"""The API client every service uses: one session, a timeout on every request, retries with backoff for
reads that hit a busy server, and errors as ApiError (carrying the HTTP status for _fatal)."""
from __future__ import annotations

from urllib.parse import urlsplit

import requests

from app.engine import clock
from app.engine.common import ApiError

BUSY = (429, 500, 502, 503, 504)


class Http:
    def __init__(self, base: str, headers: dict | None = None, *, timeout=(10, 60), retry_on=BUSY,
                 attempts: int = 3, backoff: float = 3.0, before=None):
        """GET is tried up to `attempts` times while the answer is a status in `retry_on`, waiting `backoff`,
        then twice as long each time. Writes are never retried. `before()` runs before every request
        (e.g. a rate limiter)."""
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(headers or {})
        self.timeout = timeout
        self.retry_on = retry_on
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self.before = before

    def get(self, path: str, params: dict | None = None, *, timeout=None):
        """The JSON answer. A body that isn't JSON (also an empty one) raises ValueError: the URL most likely
        points at something else."""
        delay = self.backoff
        for attempt in range(self.attempts):
            if self.before:
                self.before()
            r = self.s.get(self._url(path), params=params, timeout=timeout or self.timeout)
            if r.status_code not in self.retry_on or attempt == self.attempts - 1:
                break
            clock.sleep(delay)
            delay *= 2
        if not r.ok:  # short: these end up in track notes and logs
            raise ApiError(f"GET {path} -> {r.status_code} {r.reason or ''}".rstrip(), r.status_code)
        return r.json()

    def send(self, method: str, path: str, *, json=None, data=None, content_type: str = "",
             params: dict | None = None, timeout=None):
        """The JSON answer, {} for an empty one. `data` is sent as the body as it is, with `content_type`.
        Errors include the start of the body (Lidarr explains rejected writes there)."""
        if self.before:
            self.before()
        r = self.s.request(method, self._url(path), json=json, data=data, params=params,
                           headers={"Content-Type": content_type} if content_type else None,
                           timeout=timeout or self.timeout)
        if not r.ok:
            raise ApiError(f"{method} {path} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.text else {}

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"


IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/avif")
YOUTUBE_IMAGE_HOSTS = ("googleusercontent.com", "ytimg.com", "ggpht.com")   # where YouTube Music's artwork is


def url_allowed(url: str, hosts, schemes) -> bool:
    """`url` has one of `schemes` and its host is one of `hosts` or below it ("host" and "*.host")."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return False
    return p.scheme in schemes and bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


def fetch_image(url: str, *, max_bytes: int, timeout=(5, 10), types=IMAGE_TYPES) -> tuple[bytes, str] | None:
    """(body, content type) of the image at `url`, or None unless the answer is a 200 with one of `types` and
    1 to `max_bytes` bytes. Redirects are not followed (they could lead anywhere); a connection problem raises
    requests.RequestException."""
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=False) as r:
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if r.status_code != 200 or ctype not in types:
            return None
        body = bytearray()
        for chunk in r.iter_content(64 * 1024):
            body += chunk
            if len(body) > max_bytes:
                return None
    return (bytes(body), ctype) if body else None
