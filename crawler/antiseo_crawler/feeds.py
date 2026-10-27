"""Feed discovery and conditional feed fetching."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

import feedparser
import lxml.html

from .net import HostSession

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
FALLBACK_PATHS = ("/feed", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/rss", "/blog/feed")


@dataclass
class Entry:
    url: str
    title: str
    published: datetime | None
    html: str | None  # full content if the feed includes it, else the summary


@dataclass
class FeedResult:
    not_modified: bool = False
    entries: list[Entry] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None


def _looks_like_feed(content: bytes) -> bool:
    parsed = feedparser.parse(content)
    return bool(parsed.entries) and parsed.get("version", "") != ""


def discover(hs: HostSession) -> str | None:
    """Find a feed URL: <link rel=alternate> on the homepage, then common paths."""
    try:
        r = hs.fetch(hs.base + "/")
        if r.status_code == 200 and r.text:
            doc = lxml.html.fromstring(r.content)
            for link in doc.xpath("//link[@href]"):
                rel = (link.get("rel") or "").lower()
                typ = (link.get("type") or "").lower()
                if "alternate" in rel and typ in FEED_TYPES:
                    return urljoin(str(r.url), link.get("href"))
    except Exception:
        pass
    for path in FALLBACK_PATHS:
        try:
            r = hs.fetch(hs.base + path)
            if r.status_code == 200 and _looks_like_feed(r.content):
                return str(r.url)
        except Exception:
            continue
    return None


def _entry_date(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch(hs: HostSession, feed_url: str, etag: str | None, last_mod: str | None) -> FeedResult:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_mod:
        headers["If-Modified-Since"] = last_mod
    r = hs.fetch(feed_url, headers=headers)
    if r.status_code == 304:
        return FeedResult(not_modified=True, etag=etag, last_modified=last_mod)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    entries = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        html = None
        if e.get("content"):
            html = e.content[0].get("value")
        elif e.get("summary"):
            html = e.summary
        entries.append(
            Entry(
                url=urljoin(feed_url, link),
                title=(e.get("title") or "").strip(),
                published=_entry_date(e),
                html=html,
            )
        )
    entries.sort(key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return FeedResult(
        entries=entries,
        etag=r.headers.get("ETag"),
        last_modified=r.headers.get("Last-Modified"),
    )


def max_posts_per_day(entries: list[Entry]) -> int:
    """Highest number of posts on any single day: a content-farm signal."""
    counts: dict = {}
    for e in entries:
        if e.published:
            d = e.published.date()
            counts[d] = counts.get(d, 0) + 1
    return max(counts.values(), default=0)
