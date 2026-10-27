"""Request checks done before any route: cross-site request blocking and sign-in by a trusted reverse proxy.

Cross-site requests: browsers say where a request comes from (Sec-Fetch-Site, or at least Origin / Referer on
a POST). A state-changing request that another site made the browser send is refused, so a malicious page
can't use the session cookie (CSRF). Requests without any of these headers are not from a browser form on
another site (curl, Lidarr's webhook) and pass.

Reverse-proxy sign-in (optional): with AUTH_HEADER (e.g. Remote-User, set by Authelia / Authentik / oauth2-proxy)
and TRUSTED_PROXIES (comma-separated addresses or networks of the proxy), a request from such an address that
carries the header counts as signed in. The header is ignored from any other address, so it can't be forged by
talking to the app directly. The address is the one uvicorn sees (the proxy's, unless uvicorn itself is told to
trust forwarded headers).
"""
from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlsplit

log = logging.getLogger("playlist-sync")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_cross_site(req) -> bool:
    """A state-changing request that a page on another site made the browser send."""
    if req.method in SAFE_METHODS:
        return False
    site = req.headers.get("sec-fetch-site")
    if site is not None:  # every current browser sends it
        return site not in ("same-origin", "none")
    origin = req.headers.get("origin") or req.headers.get("referer")
    if origin is None:
        return False  # not a browser form on another site
    if origin == "null":
        return True
    hosts = {h.strip().lower() for h in (req.headers.get("host", ""), req.headers.get("x-forwarded-host", ""))
             if h.strip()}
    return urlsplit(origin).netloc.lower() not in hosts


def _proxy_config() -> tuple[str, list]:
    header = os.environ.get("AUTH_HEADER", "").strip()
    nets = []
    for part in os.environ.get("TRUSTED_PROXIES", "").split(","):
        if part.strip():
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                log.warning("TRUSTED_PROXIES: ignoring %r (not an address or network)", part.strip())
    return header, nets


def proxy_auth_configured() -> bool:
    header, nets = _proxy_config()
    return bool(header and nets)


def proxy_user(req) -> str:
    """The user a trusted reverse proxy signed in ('' if none)."""
    header, nets = _proxy_config()
    if not (header and nets and req.client):
        return ""
    try:
        addr = ipaddress.ip_address(req.client.host)
    except ValueError:
        return ""
    if not any(addr in net for net in nets):
        return ""
    return req.headers.get(header, "").strip()
