"""Settings, all from environment variables (set in ~/.antiseo/crawler.env on the Pi).

Postgres uses the standard libpq variables, which psycopg reads on its own:
PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGSSLMODE.
"""
import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    model_dir: str = os.environ.get("MODEL_DIR", "/models/minilm")
    user_agent: str = os.environ.get(
        "USER_AGENT", "AntiSEOBot/0.1 (+https://search.theincorrect.one/bot)"
    )
    robots_token: str = "AntiSEOBot"
    workers: int = _int("WORKERS", 4)            # domains crawled in parallel
    delay: float = _float("DOWNLOAD_DELAY", 2.0)  # seconds between requests to one host
    timeout: float = _float("HTTP_TIMEOUT", 20.0)
    max_bytes: int = _int("MAX_BYTES", 5_000_000)
    max_posts: int = _int("MAX_POSTS_PER_DOMAIN", 30)
    min_words: int = _int("MIN_WORDS", 300)
    embed_threads: int = _int("EMBED_THREADS", 3)
    embed_words: int = _int("EMBED_WORDS", 200)   # title + first N words -> one vector
    batch_size: int = _int("UPSERT_BATCH", 200)
    hc_url: str = os.environ.get("HC_URL", "")    # Healthchecks.io ping URL (optional)
    # Only for local testing. Never set this on the Pi: it lets feeds point the
    # crawler at your home network.
    allow_private: bool = os.environ.get("ALLOW_PRIVATE_ADDRESSES") == "1"


CFG = Config()
