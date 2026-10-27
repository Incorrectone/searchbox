"""Hybrid search from the command line (the same query the API will run).

  search "memory leak debugging in rust"
  search --bench queries.txt        # one query per line: the Phase 1 exit test
"""
from __future__ import annotations

import argparse
import time

from . import db
from .config import CFG
from .embedder import Embedder, to_pgvector

# Reciprocal Rank Fusion of a semantic (HNSW) and a keyword (tsvector) ranking.
# hnsw.ef_search must be >= the inner LIMIT, or HNSW returns only 40 candidates.
RRF_SQL = """
WITH sem AS (
  SELECT id, row_number() OVER (ORDER BY dist) AS r FROM (
    SELECT id, embedding <=> %(v)s::halfvec(384) AS dist
    FROM documents ORDER BY dist LIMIT 100) s),
kw AS (
  SELECT id, row_number() OVER (ORDER BY rank DESC) AS r FROM (
    SELECT id, ts_rank_cd(tsv, websearch_to_tsquery('english', %(q)s)) AS rank
    FROM documents WHERE tsv @@ websearch_to_tsquery('english', %(q)s)
    ORDER BY rank DESC LIMIT 100) k)
SELECT d.title, d.url, d.published_at, SUM(1.0 / (60 + u.r)) AS score,
       bool_or(u.src = 'sem') AS in_sem, bool_or(u.src = 'kw') AS in_kw
FROM (SELECT id, r, 'sem' AS src FROM sem UNION ALL SELECT id, r, 'kw' FROM kw) u
JOIN documents d USING (id)
GROUP BY d.id ORDER BY score DESC
LIMIT %(k)s
"""


def search(conn, emb: Embedder, query: str, k: int):
    vec = to_pgvector(emb.embed([query])[0])
    with conn.transaction():
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        return conn.execute(RRF_SQL, {"v": vec, "q": query, "k": k}).fetchall()


def show(query: str, rows, ms: float) -> None:
    print(f'\n=== "{query}"  ({len(rows)} results, {ms:.0f} ms)')
    for i, (title, url, published, score, in_sem, in_kw) in enumerate(rows, 1):
        how = ("S" if in_sem else "-") + ("K" if in_kw else "-")
        date = published.date().isoformat() if published else "    ?     "
        print(f"{i:2}. [{how}] {score:.4f} {date}  {title[:70]}")
        print(f"              {url}")


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="search", description="Hybrid RRF search")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--bench", help="file with one query per line")
    ap.add_argument("-k", type=int, default=10)
    args = ap.parse_args(argv)
    if not args.query and not args.bench:
        ap.error("give a query or --bench FILE")

    emb = Embedder(CFG.model_dir, CFG.embed_threads)
    with db.connect() as conn:
        db.check_model(conn, emb.sha256)
        print(f"index: {db.get_meta(conn, 'doc_count')} docs, version {db.get_meta(conn, 'index_version')}")
        queries = [args.query] if args.query else [
            q.strip() for q in open(args.bench, encoding="utf-8") if q.strip() and not q.startswith("#")
        ]
        for q in queries:
            t = time.perf_counter()
            rows = search(conn, emb, q, args.k if args.query else 5)
            show(q, rows, (time.perf_counter() - t) * 1000)
    print("\n[S-] semantic only  [-K] keyword only  [SK] both")
    return 0
