"""The API client every service uses: one session, a timeout on every request, retries with backoff for
reads that hit a busy server, and errors as ApiError (carrying the HTTP status for _fatal)."""
from __future__ import annotations

import requests

from app.engine import clock
from app.engine.common import ApiError

BUSY = (429, 500, 502, 503, 504)


class Http:
    def __init__(self, base: str, headers: dict | None = None, *, timeout=(10, 60), retry_on=BUSY,
                 attempts: int = 3, backoff: float = 3.0):
        """GET is tried up to `attempts` times while the answer is a status in `retry_on`, waiting `backoff`,
        then twice as long each time. Writes are never retried."""
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(headers or {})
        self.timeout = timeout
        self.retry_on = retry_on
        self.attempts = max(1, attempts)
        self.backoff = backoff

    def get(self, path: str, params: dict | None = None, *, timeout=None):
        delay = self.backoff
        for attempt in range(self.attempts):
            r = self.s.get(self._url(path), params=params, timeout=timeout or self.timeout)
            if r.status_code not in self.retry_on or attempt == self.attempts - 1:
                break
            clock.sleep(delay)
            delay *= 2
        return self._result("GET", path, r)

    def send(self, method: str, path: str, *, json=None, params: dict | None = None, timeout=None):
        r = self.s.request(method, self._url(path), json=json, params=params, timeout=timeout or self.timeout)
        return self._result(method, path, r)

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    @staticmethod
    def _result(method: str, path: str, r):
        if not r.ok:
            raise ApiError(f"{method} {path} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.text else {}
