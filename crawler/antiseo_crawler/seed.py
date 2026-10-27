"""Add domains to crawl.

  seed --kagi 50            random sample of 50 feeds from Kagi's Small Web list
  seed --file my_blogs.txt  one host or feed URL per line (# comments allowed)
"""
from __future__ import annotations

import argparse
import random
from urllib.parse import urlsplit

from . import db
from .net import make_client

KAGI_LIST = "https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt"

EXCLUDE = (
    "medium.com", "dev.to", "hashnode.dev", "hashnode.com", "substack.com",
    "geeksforgeeks.org", "w3schools.com", "tutorialspoint.com", "javatpoint.com",
    "blogspot.com", "wordpress.com", "youtube.com", "github.com",
)


def _excluded(host: str) -> bool:
    return any(host == x or host.endswith("." + x) for x in EXCLUDE)


def _parse(line: str) -> tuple[str, str | None] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:  # a feed URL
        host = urlsplit(line).netloc.lower()
        return (host.removeprefix("www."), line) if host else None
    return line.lower().strip("/").removeprefix("www."), None


def add(conn, items: list[tuple[str, str | None]], source: str) -> int:
    added = 0
    for host, feed_url in items:
        if _excluded(host):
            continue
        cur = conn.execute(
            "INSERT INTO domains (host, source, feed_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (host) DO NOTHING",
            (host, source, feed_url),
        )
        added += cur.rowcount
    conn.commit()
    return added


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seed", description="Add domains to crawl")
    ap.add_argument("--kagi", type=int, metavar="N", help="sample N feeds from Kagi Small Web")
    ap.add_argument("--file", help="hosts or feed URLs, one per line")
    ap.add_argument("--random-seed", type=int, default=42)
    args = ap.parse_args(argv)
    if not args.kagi and not args.file:
        ap.error("use --kagi N and/or --file FILE")

    with db.connect() as conn:
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                items = [p for p in map(_parse, f) if p]
            print(f"file: added {add(conn, items, 'manual')} of {len(items)}")
        if args.kagi:
            with make_client() as client:
                lines = client.get(KAGI_LIST).text.splitlines()
            items = [p for p in map(_parse, lines) if p and not _excluded(p[0])]
            random.Random(args.random_seed).shuffle(items)
            print(f"kagi: added {add(conn, items[: args.kagi], 'kagi_smallweb')} of {args.kagi} sampled "
                  f"({len(items)} in list)")
        total = conn.execute("SELECT count(*) FROM domains WHERE status='active'").fetchone()[0]
        print(f"{total} active domains")
    return 0
