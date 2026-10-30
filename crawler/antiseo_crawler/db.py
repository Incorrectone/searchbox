"""Postgres access. Connection settings come from the PG* environment variables."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from .embedder import to_pgvector
from .models import Candidate


@dataclass
class Domain:
    id: int
    host: str
    feed_url: str | None
    feed_etag: str | None
    feed_last_mod: str | None


def connect() -> psycopg.Connection:
    return psycopg.connect("", connect_timeout=15)


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def check_model(conn, sha256: str) -> None:
    """Refuse to mix embeddings from two different model files."""
    stored = get_meta(conn, "embedding_model")
    if stored is None:
        set_meta(conn, "embedding_model", sha256)
        conn.commit()
    elif stored != sha256:
        raise SystemExit(
            f"Model mismatch: DB was built with {stored[:12]}..., this model is "
            f"{sha256[:12]}... Use the original model.onnx, or rebuild the index."
        )


def active_domains(conn, only_host: str | None = None, limit: int | None = None) -> list[Domain]:
    sql = (
        "SELECT id, host, feed_url, feed_etag, feed_last_mod FROM domains "
        "WHERE status = 'active'"
    )
    params: list = []
    if only_host:
        sql += " AND host = %s"
        params.append(only_host)
    sql += " ORDER BY last_crawled NULLS FIRST, id"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return [Domain(*r) for r in conn.execute(sql, params).fetchall()]


def known_urls(conn) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for domain_id, url in conn.execute("SELECT domain_id, url FROM documents"):
        out.setdefault(domain_id, set()).add(url)
    return out


def update_domain(conn, d: Domain, feed_url, etag, last_mod) -> None:
    conn.execute(
        "UPDATE domains SET feed_url = %s, feed_etag = %s, feed_last_mod = %s, "
        "last_crawled = now() WHERE id = %s",
        (feed_url, etag, last_mod, d.id),
    )


def content_hash(text: str) -> bytes:
    return hashlib.sha256(" ".join(text.split()).lower().encode()).digest()


_UPSERT = """
INSERT INTO documents
  (domain_id, url, title, published_at, summary, body, content_hash, quality_score, embedding)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::halfvec(384))
ON CONFLICT (url) DO UPDATE SET
  title = EXCLUDED.title, published_at = EXCLUDED.published_at,
  summary = EXCLUDED.summary, body = EXCLUDED.body,
  content_hash = EXCLUDED.content_hash, quality_score = EXCLUDED.quality_score,
  embedding = EXCLUDED.embedding, crawled_at = now()
WHERE documents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
"""


def upsert(conn, cands: list[Candidate], vectors) -> int:
    rows = []
    for c, v in zip(cands, vectors):
        summary = " ".join(c.text.split())[:300]
        rows.append(
            (c.domain_id, c.url, c.title, c.published_at, summary, c.text,
             content_hash(c.text), c.quality_score, to_pgvector(v))
        )
    with conn.cursor() as cur:
        cur.executemany(_UPSERT, rows)
    return len(rows)


_IMAGE_UPSERT = """
INSERT INTO images (document_id, source_url, blob_path, width, height, alt)
SELECT id, %s, %s, %s, %s, %s FROM documents WHERE url = %s
ON CONFLICT (document_id) DO UPDATE SET
  source_url = EXCLUDED.source_url, blob_path = EXCLUDED.blob_path,
  width = EXCLUDED.width, height = EXCLUDED.height, alt = EXCLUDED.alt
"""


def upsert_images(conn, cands: list[Candidate]) -> None:
    rows = [
        (t.source_url, t.blob_path, t.width, t.height, t.alt, c.url)
        for c in cands
        if (t := c.thumbnail) is not None
    ]
    if rows:
        with conn.cursor() as cur:
            cur.executemany(_IMAGE_UPSERT, rows)


def finish_run(conn, inserted: int) -> int:
    """Bump index_version (invalidates cached searches) and record stats."""
    version = int(get_meta(conn, "index_version") or 0)
    if inserted:
        version += 1
        set_meta(conn, "index_version", str(version))
    count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    set_meta(conn, "doc_count", str(count))
    set_meta(conn, "last_crawl_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    conn.commit()
    return version
