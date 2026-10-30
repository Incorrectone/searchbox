"""The weekly crawl: feeds -> new posts -> clean text -> quality gate -> embed -> Postgres.

Fetching runs in a few threads (one domain per thread, polite delay per host).
Embedding and database writes happen on the main thread.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from . import db, feeds, images, quality
from .config import CFG
from .embedder import Embedder
from .extract import extract, looks_english, word_count
from .models import Candidate
from .net import BackOff, BlockedURL, HostSession, get, make_client, same_site


@dataclass
class DomainResult:
    domain: db.Domain
    feed_url: str | None = None
    etag: str | None = None
    last_mod: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    status: str = "ok"


def _skip(r: DomainResult, reason: str) -> None:
    r.skipped[reason] = r.skipped.get(reason, 0) + 1


def _base_url(client: httpx.Client, host: str) -> str:
    """Prefer https; fall back to http for the odd old blog."""
    for scheme in ("https", "http"):
        try:
            resp = client.head(f"{scheme}://{host}/")
            if resp.status_code < 500:
                return f"{scheme}://{host}"
        except Exception:
            continue
    return f"https://{host}"


def process_domain(client: httpx.Client, d: db.Domain, known: set[str], max_posts: int) -> DomainResult:
    r = DomainResult(domain=d, feed_url=d.feed_url, etag=d.feed_etag, last_mod=d.feed_last_mod)
    try:
        hs = HostSession(client, _base_url(client, d.host))
        if not r.feed_url:
            r.feed_url = feeds.discover(hs)
            if not r.feed_url:
                r.status = "no feed found"
                return r
        fr = feeds.fetch(hs, r.feed_url, d.feed_etag, d.feed_last_mod)
        if fr.not_modified:
            r.status = "feed unchanged"
            return r
        r.etag, r.last_mod = fr.etag, fr.last_modified
        burst = feeds.max_posts_per_day(fr.entries)

        for e in fr.entries[:max_posts]:
            if e.url in known:
                _skip(r, "already indexed")
                continue
            if not same_site(e.url, d.host):
                _skip(r, "off-site link")
                continue
            html, full_page = e.html, False
            ex = extract(html, e.url) if html else None
            if not ex or word_count(ex.text) < CFG.min_words:
                # Feed only has a summary: fetch the article itself.
                try:
                    resp = hs.fetch(e.url)
                except BlockedURL:
                    _skip(r, "blocked/robots")
                    continue
                if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                    _skip(r, f"http {resp.status_code}")
                    continue
                html, full_page = resp.text, True
                ex = extract(html, str(resp.url))
            if not ex:
                _skip(r, "no text")
                continue
            if word_count(ex.text) < CFG.min_words:
                _skip(r, f"< {CFG.min_words} words")
                continue
            if not looks_english(ex.text):
                _skip(r, "not English")
                continue
            cand = Candidate(
                domain_id=d.id,
                host=d.host,
                url=e.url,
                title=(e.title or ex.title or e.url)[:500],
                text=ex.text,
                published_at=e.published or ex.date,
                html=html or "",
                html_is_full_page=full_page,
                feed_max_posts_per_day=burst,
            )
            if images.images_enabled():
                picked = images.pick(cand.html, e.url)
                if picked:
                    img_url, alt = picked

                    def fetch(u, _hs=hs, _host=d.host):
                        return _hs.fetch(u) if same_site(u, _host) else get(client, u)

                    cand.thumbnail = images.fetch_thumbnail(fetch, img_url, alt or cand.title)
            r.candidates.append(cand)
    except BackOff as ex:
        r.status = f"backed off ({ex})"
    except BlockedURL as ex:
        r.status = f"blocked ({ex})"
    except Exception as ex:  # one broken blog must never stop the run
        r.status = f"error ({type(ex).__name__}: {str(ex)[:120]})"
    return r


def ping_healthcheck(ok: bool) -> None:
    if not CFG.hc_url:
        return
    try:
        httpx.get(CFG.hc_url + ("" if ok else "/fail"), timeout=10)
    except Exception:
        pass


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crawl", description="Run the feed crawl")
    ap.add_argument("--domain", help="crawl only this host")
    ap.add_argument("--limit", type=int, help="crawl at most N domains")
    ap.add_argument("--max-posts", type=int, default=CFG.max_posts)
    ap.add_argument("--dry-run", action="store_true", help="fetch and score, write nothing")
    args = ap.parse_args(argv)

    t0 = time.monotonic()
    emb = Embedder(CFG.model_dir, CFG.embed_threads)
    conn = db.connect()
    try:
        db.check_model(conn, emb.sha256)
        domains = db.active_domains(conn, args.domain, args.limit)
        known = db.known_urls(conn)
        print(f"crawling {len(domains)} domains (model {emb.sha256[:12]})", flush=True)

        kept_total, dropped_total, pending = 0, 0, []
        img_uploaded, img_budget = 0, CFG.max_images_per_run
        upload_client = make_client() if images.images_enabled() else None

        def flush():
            nonlocal kept_total, img_uploaded
            if not pending:
                return
            texts = [f"{c.title}\n{' '.join(c.text.split()[:CFG.embed_words])}" for c in pending]
            vecs = emb.embed(texts)
            if not args.dry_run:
                db.upsert(conn, pending, vecs)
                with_images = []
                for c in pending:
                    if c.thumbnail is not None and img_uploaded < img_budget and upload_client:
                        try:
                            ok = images.upload(upload_client, c.thumbnail)
                        except Exception:
                            ok = False
                        if ok:
                            img_uploaded += 1
                            with_images.append(c)
                db.upsert_images(conn, with_images)
                conn.commit()
            kept_total += len(pending)
            pending.clear()

        with make_client() as client, ThreadPoolExecutor(CFG.workers) as pool:
            jobs = [pool.submit(process_domain, client, d, known.get(d.id, set()), args.max_posts) for d in domains]
            for job in jobs:
                r = job.result()
                kept = 0
                for c in r.candidates:
                    q = quality.assess(c)
                    c.quality_score = q.score
                    c.quality_reasons = q.reasons
                    if q.keep:
                        pending.append(c)
                        kept += 1
                    else:
                        dropped_total += 1
                        print(f"  DROP {c.url}  score={q.score:.1f}  {'; '.join(q.reasons)}", flush=True)
                if not args.dry_run:
                    db.update_domain(conn, r.domain, r.feed_url, r.etag, r.last_mod)
                    conn.commit()
                skipped = ", ".join(f"{k}: {v}" for k, v in r.skipped.items())
                print(f"{r.domain.host:40} {r.status:16} +{kept}  {skipped}", flush=True)
                if len(pending) >= CFG.batch_size:
                    flush()
        flush()

        version = db.finish_run(conn, kept_total) if not args.dry_run else None
        mins = (time.monotonic() - t0) / 60
        if upload_client:
            upload_client.close()
        print(
            f"done in {mins:.1f} min: {kept_total} new docs, {img_uploaded} thumbnails, "
            f"{dropped_total} dropped by quality gate"
            + (f", index_version={version}" if version is not None else " (dry run)"),
            flush=True,
        )
        ping_healthcheck(True)
        return 0
    except BaseException:
        ping_healthcheck(False)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run())
