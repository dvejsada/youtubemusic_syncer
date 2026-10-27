"""Request checks done before any route: cross-site request blocking and sign-in by a trusted reverse proxy.

Cross-site requests: browsers say where a request comes from (Sec-Fetch-Site, or at least Origin / Referer on
a POST). A state-changing request that another site made the browser send is refused, so a malicious page
can't use the session cookie (CSRF). Requests without any of these headers are not from a browser form on
another site (curl, Lidarr's webhook) and pass.

Reverse-proxy sign-in (optional): with AUTH_HEADER (e.g. Remote-User, set by Authelia / Authentik / oauth2-proxy)
and TRUSTED_PROXIES (comma-separated addresses or networks of the proxy), a request from such an address that
carries the header counts as signed in. The header is ignored from any other address, so it can't be forged by
talking to the app directly. That relies on the address being the real peer of the connection: uvicorn must not
rewrite it from X-Forwarded-For (run it with --no-proxy-headers, as the Docker image does). With
FORWARDED_ALLOW_IPS set, which tells uvicorn to trust those headers, proxy sign-in stays off.
"""
from __future__ import annotations

import functools
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
    forwarded = req.headers.get("x-forwarded-host", "").split(",")  # chained proxies list several
    hosts = {h.strip().lower() for h in (req.headers.get("host", ""), *forwarded) if h.strip()}
    return urlsplit(origin).netloc.lower() not in hosts


def _proxy_config() -> tuple[str, tuple]:
    return _parse_proxy_config(os.environ.get("AUTH_HEADER", ""), os.environ.get("TRUSTED_PROXIES", ""),
                               os.environ.get("FORWARDED_ALLOW_IPS", ""))


@functools.lru_cache(maxsize=8)  # parsed (and complained about) once per value, not on every request
def _parse_proxy_config(header: str, proxies: str, forwarded_allow: str) -> tuple[str, tuple]:
    header = header.strip()
    if not header or not proxies.strip():
        return "", ()
    if forwarded_allow.strip():
        log.error("Proxy sign-in is off: FORWARDED_ALLOW_IPS is set, so uvicorn may replace the client address "
                  "with one taken from X-Forwarded-For and the trusted-proxy check could be forged.")
        return "", ()
    nets = []
    for part in (p.strip() for p in proxies.split(",")):
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part)  # strict: "10.1.2.3/8" is a typo, not 10.0.0.0/8
        except ValueError:
            log.warning("TRUSTED_PROXIES: ignoring %r (not an address or network; a network must be written "
                        "with its first address, e.g. 10.0.0.0/8)", part)
            continue
        if net.prefixlen == 0:
            log.warning("TRUSTED_PROXIES: ignoring %r: it would trust every address", part)
            continue
        if net.num_addresses > 1:
            log.warning("TRUSTED_PROXIES: %r is a network: every host in it can sign in as anyone, including its "
                        "gateway, which is where Docker's published ports can forward outside connections from. "
                        "Prefer the proxy's own address.", part)
        nets.append(net)
    return header, tuple(nets)


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
    addr = getattr(addr, "ipv4_mapped", None) or addr  # ::ffff:10.0.0.5 when uvicorn listens on IPv6
    if not any(addr in net for net in nets):
        return ""
    return req.headers.get(header, "").strip()
