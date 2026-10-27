"""Helpers shared by the service clients: secret redaction, error formatting, URLs."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests

from app.engine.models import EngineError, Settings


_QUERY = re.compile(r"(?<=[\w/])\?(?=[^\s)'\"]*=)[^\s)'\"]+")  # only real "?key=value" query strings


def _redactor(settings: Settings):
    """Returns f(text) that removes API keys and URL query strings from text."""
    secrets = [s for s in (settings.lidarr_api_key, settings.jellyfin_api_key) if s and len(s) >= 4]

    def redact(text) -> str:
        text = str(text)
        for sec in secrets:
            text = text.replace(sec, "***")
        return _QUERY.sub("", text)
    return redact


def _origin(url: str) -> str:
    """scheme://host:port of a URL (drops credentials, path and query)."""
    try:
        p = urlsplit(url if "://" in url else "http://" + url)
        host = p.hostname or url
        return f"{p.scheme}://{host}" + (f":{p.port}" if p.port else "")
    except ValueError:
        return "the configured URL"


def _base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


class ApiError(RuntimeError):
    """HTTP error answer from a service (raised by http.Http; carries the status code for _fatal)."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def _fatal(service: str, exc: Exception, url: str, redact) -> EngineError:
    """Turn any exception from connecting to a service into a clear, secret-free EngineError."""
    if isinstance(exc, EngineError):
        return exc
    status = getattr(exc, "status", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        word = "Unauthorized" if status == 401 else "Forbidden"
        return EngineError(f"{service}: {status} {word} — check the API key")
    if status == 404:
        return EngineError(f"{service}: 404 Not Found — check the URL")
    if status:
        return EngineError(f"{service}: HTTP {status} from {_origin(url)}")
    if isinstance(exc, requests.exceptions.Timeout):
        return EngineError(f"{service}: timed out connecting to {_origin(url)}")
    if isinstance(exc, requests.exceptions.ConnectionError):
        return EngineError(f"{service}: cannot reach {_origin(url)} — check the URL and that it is running")
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                        requests.exceptions.InvalidSchema)):
        return EngineError(f"{service}: invalid URL")
    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError, AttributeError)):
        # JSON decode error / unexpected JSON shape: answered, but not by the service we expect
        return EngineError(f"{service}: unexpected response from {_origin(url)} — is the URL right?")
    return EngineError(f"{service}: {redact(exc)}")


_NOLOG = lambda m="": None  # noqa: E731

_CHECK_TIMEOUT = (5, 15)  # connection checks in Settings
