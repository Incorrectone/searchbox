"""HTTP client with home-network protection and robots.txt handling.

The crawler runs on your home network and fetches URLs that strangers control
(every feed decides where its links point). Without a guard, a feed could link
to http://192.168.1.1/ and make the crawler request your router's admin page.
So every request, including each redirect hop, is checked: the hostname is
resolved and anything that isn't a public internet address is refused
(private LAN ranges, loopback, link-local, Tailscale's 100.64.0.0/10, etc.).

Known gap: DNS could change between this check and the actual connection
(DNS rebinding). Acceptable for a demo; the container's other limits still apply.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import CFG


class BlockedURL(Exception):
    pass


def _check_public(request: httpx.Request) -> None:
    if request.url.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {request.url.scheme}")
    if CFG.allow_private:
        return
    host = request.url.host
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise BlockedURL(f"DNS failed for {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise BlockedURL(f"{host} resolves to non-public {ip}")


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": CFG.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/rss+xml,"
            "application/atom+xml,application/xml;q=0.9,*/*;q=0.5",
        },
        timeout=CFG.timeout,
        follow_redirects=True,
        max_redirects=5,
        event_hooks={"request": [_check_public]},  # runs for every redirect hop too
    )


class BackOff(Exception):
    """The site is rate limiting or challenging us: stop crawling this domain."""


def get(client: httpx.Client, url: str, headers: dict | None = None) -> httpx.Response:
    resp = client.get(url, headers=headers or {})
    if resp.status_code in (403, 429, 503) or resp.headers.get("cf-mitigated"):
        raise BackOff(f"{resp.status_code} from {url}")
    if len(resp.content) > CFG.max_bytes:
        raise BlockedURL(f"response too large ({len(resp.content)} bytes): {url}")
    return resp


class HostSession:
    """Per-domain helper: robots.txt rules plus a fixed delay between requests."""

    def __init__(self, client: httpx.Client, base: str):
        self.client = client
        self.base = base.rstrip("/")
        self._last = 0.0
        self._lock = threading.Lock()
        self.robots = RobotFileParser()
        try:
            r = self.fetch(self.base + "/robots.txt", check_robots=False)
            if r.status_code == 200:
                self.robots.parse(r.text.splitlines())
            else:
                self.robots.parse([])  # no robots.txt -> everything allowed
        except BackOff:
            raise
        except Exception:
            self.robots.parse([])

    def allowed(self, url: str) -> bool:
        return self.robots.can_fetch(CFG.robots_token, url)

    def fetch(self, url: str, headers: dict | None = None, check_robots: bool = True):
        if check_robots and not self.allowed(url):
            raise BlockedURL(f"robots.txt disallows {url}")
        with self._lock:
            wait = self._last + CFG.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                return get(self.client, url, headers)
            finally:
                self._last = time.monotonic()


def same_site(url: str, host: str) -> bool:
    """True if url is on host or one of its subdomains (www. etc.)."""
    h = (urlsplit(url).netloc or "").lower()
    host = host.lower()
    return h == host or h.endswith("." + host) or host.endswith("." + h)
