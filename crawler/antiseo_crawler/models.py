from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Candidate:
    """One extracted post, before the quality gate decides whether to keep it."""

    domain_id: int
    host: str
    url: str
    title: str
    text: str                    # clean article text (already >= MIN_WORDS, English)
    published_at: datetime | None
    html: str                    # raw HTML: the full page if fetched, else the feed's content
    html_is_full_page: bool      # False when the text came straight from the feed
    feed_max_posts_per_day: int  # highest posts-on-one-day count in this feed
    quality_score: float | None = None
    quality_reasons: list[str] = field(default_factory=list)
