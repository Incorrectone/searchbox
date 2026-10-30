"""Backfill thumbnails for posts that are already indexed but have none.

The weekly crawl only makes thumbnails for NEW posts. Posts indexed before
BLOB_BASE_URL/BLOB_SAS were set (or whose image failed) are fixed with:

    deploy/run.sh thumbs              # up to MAX_IMAGES_PER_RUN posts
    deploy/run.sh thumbs --limit 20   # a small test first
    deploy/run.sh thumbs --domain example.com

Each post page is fetched again through the same rules as the crawl (robots.txt,
per-site delay, home-network guard), then the same pick -> sanitize -> upload
path is used. Posts with no usable image are simply left without one; running
it again retries them.
"""
from __future__ import annotations

import argparse
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from . import db, images
from .config import CFG
from .crawl import _base_url
from .net import BackOff, BlockedURL, HostSession, get, make_client, same_site

_MISSING = """
SELECT d.id, d.url, d.title, dm.host
FROM documents d
JOIN domains dm ON dm.id = d.domain_id
LEFT JOIN images i ON i.document_id = d.id
WHERE i.document_id IS NULL AND (%(host)s::text IS NULL OR dm.host = %(host)s)
ORDER BY dm.host, d.id
LIMIT %(limit)s
"""


def _host_job(client, host: str, posts: list[tuple[str, str]]):
    """Fetch each post of one blog and make its thumbnail. Runs in a worker thread."""
    out, status = [], "ok"
    try:
        hs = HostSession(client, _base_url(client, host))

        def fetch(u):
            return hs.fetch(u) if same_site(u, host) else get(client, u)

        for url, title in posts:
            try:
                resp = hs.fetch(url)
            except BlockedURL:
                continue
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                continue
            picked = images.pick(resp.text, str(resp.url))
            if not picked:
                continue
            img_url, alt = picked
            thumb = images.fetch_thumbnail(fetch, img_url, alt or title)
            if thumb:
                out.append(SimpleNamespace(url=url, thumbnail=thumb))
    except BackOff as ex:
        status = f"backed off ({ex})"
    except BlockedURL as ex:
        status = f"blocked ({ex})"
    except Exception as ex:  # one broken blog must never stop the run
        status = f"error ({type(ex).__name__}: {str(ex)[:120]})"
    return host, len(posts), out, status


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="thumbs", description="Backfill thumbnails for indexed posts")
    ap.add_argument("--limit", type=int, default=CFG.max_images_per_run, help="at most N posts")
    ap.add_argument("--domain", help="only this host")
    args = ap.parse_args(argv)

    if not images.images_enabled():
        print("BLOB_BASE_URL and BLOB_SAS are not set in crawler.env; nothing to do.")
        return 1

    t0 = time.monotonic()
    conn = db.connect()
    try:
        rows = conn.execute(_MISSING, {"host": args.domain, "limit": args.limit}).fetchall()
        by_host: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for _id, url, title, host in rows:
            by_host[host].append((url, title))
        print(f"{len(rows)} posts without a thumbnail on {len(by_host)} blogs", flush=True)

        made = 0
        with make_client() as client, make_client() as upload_client, ThreadPoolExecutor(CFG.workers) as pool:
            jobs = [pool.submit(_host_job, client, h, p) for h, p in by_host.items()]
            for job in jobs:
                host, tried, found, status = job.result()
                uploaded = []
                for c in found:
                    try:
                        if images.upload(upload_client, c.thumbnail):
                            uploaded.append(c)
                    except Exception:
                        pass
                db.upsert_images(conn, uploaded)
                conn.commit()
                made += len(uploaded)
                note = "" if status == "ok" else f"  [{status}]"
                print(f"  {host:<40} {len(uploaded)}/{tried}{note}", flush=True)

        # New thumbnails change what /images returns, so invalidate cached results.
        if made:
            version = int(db.get_meta(conn, "index_version") or 0) + 1
            db.set_meta(conn, "index_version", str(version))
            conn.commit()
        print(f"done in {(time.monotonic() - t0) / 60:.1f} min: {made} thumbnails", flush=True)
    finally:
        conn.close()
    return 0
