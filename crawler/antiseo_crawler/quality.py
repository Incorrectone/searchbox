"""Anti-SEO quality gate.

Every post that survives extraction (English, >= MIN_WORDS words) comes through
`assess()`, which returns a score, a keep/drop decision and human-readable
reasons, so the crawl log explains every decision:
    DROP https://x.example/top-10-vpns  score=-4.0  listicle/SEO title; 3 ad/affiliate signals

Scoring: start at 0, add penalties (and one small bonus). A post is kept if
score >= THRESHOLD. One weak signal alone never drops a post; it takes a
combination (e.g. an SEO title on an ad-heavy page).

Signals (plan §4.4):
  - ad / affiliate scripts and links    -1 each distinct network, max -4
                                        (only visible when the full page was fetched;
                                        feed content has no <script> tags)
  - listicle / SEO-bait title           -1.5
  - LLM boilerplate phrases in the text -0.5 each distinct phrase, max -2.5
  - posting burst (content farm)        -2 if > 5 posts on one day, -3 if > 10
  + author markup (rel=author / meta)   +0.5
Every seeded domain has a feed by construction, so "has a feed" is implicit.

Tune THRESHOLD and the lists against your test queries:
  crawl, then `search --bench /data/queries.txt`, look at what got in, adjust.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Candidate

THRESHOLD = -2.5  # keep if score >= THRESHOLD

AD_PATTERNS = {
    "googlesyndication": re.compile(r"googlesyndication\.com|adsbygoogle", re.I),
    "doubleclick": re.compile(r"doubleclick\.net|googletagservices\.com", re.I),
    "amazon-ads": re.compile(r"amazon-adsystem\.com", re.I),
    "amazon-affiliate": re.compile(r"amazon\.[a-z.]+/[^\"'\s]*[?&]tag=[\w-]+", re.I),
    "taboola": re.compile(r"taboola\.com", re.I),
    "outbrain": re.compile(r"outbrain\.com", re.I),
    "ezoic": re.compile(r"ezoic\.(net|com)|ezojs", re.I),
    "mediavine": re.compile(r"mediavine\.com", re.I),
    "media.net": re.compile(r"media\.net/|contextual\.media\.net", re.I),
    "mgid": re.compile(r"mgid\.com", re.I),
    "affiliate-networks": re.compile(r"shareasale\.com|awin1\.com|anrdoezrs\.net|tkqlhce\.com|jdoqocy\.com|go\.skimresources\.com", re.I),
}

TITLE_PATTERNS = [
    re.compile(r"^\s*(the\s+)?(top|best)\s+\d+\b", re.I),                      # "Top 10 ...", "Best 7 ..."
    re.compile(r"\b\d+\s+(best|top|ways|tips|reasons|things|tools|hacks|mistakes|secrets)\b", re.I),
    re.compile(r"\bbest\b.{0,40}\b(in|for)\s+20\d\d\b", re.I),               # "best X in 2026"
    re.compile(r"\b(ultimate|complete|definitive)\s+guide\b", re.I),
    re.compile(r"\b(everything\s+)?you\s+(need|must)\s+to\s+know\b", re.I),
    re.compile(r"\byou\s+won'?t\s+believe\b", re.I),
]

LLM_PHRASES = [
    "in today's fast-paced",
    "in today’s fast-paced",
    "delve into",
    "delving into",
    "as an ai language model",
    "as an ai",
    "in the ever-evolving",
    "ever-changing landscape",
    "it's important to note that",
    "it is important to note that",
    "navigating the complexities",
    "a testament to",
    "unlock the full potential",
    "in conclusion,",
    "tapestry of",
    "embark on a journey",
]

AUTHOR_MARKUP = re.compile(r'rel=["\']author["\']|<meta[^>]+name=["\']author["\']', re.I)


@dataclass
class QualityResult:
    score: float
    keep: bool
    reasons: list[str] = field(default_factory=list)


def assess(c: Candidate) -> QualityResult:
    score = 0.0
    reasons: list[str] = []

    # Ads and affiliate links: one point per distinct network, capped.
    html = c.html or ""
    ad_hits = [name for name, rx in AD_PATTERNS.items() if rx.search(html)]
    if ad_hits:
        penalty = min(len(ad_hits), 4)
        score -= penalty
        reasons.append(f"{len(ad_hits)} ad/affiliate signals ({', '.join(ad_hits[:4])})")

    # SEO-bait / listicle title.
    if any(rx.search(c.title or "") for rx in TITLE_PATTERNS):
        score -= 1.5
        reasons.append("listicle/SEO title")

    # LLM boilerplate: distinct phrases, capped.
    text = (c.text or "").lower()
    found = sorted({p for p in LLM_PHRASES if p in text})
    if found:
        penalty = min(0.5 * len(found), 2.5)
        score -= penalty
        reasons.append(f"{len(found)} boilerplate phrases ({', '.join(found[:3])})")

    # Posting burst: humans rarely publish more than a handful of posts in a day.
    if c.feed_max_posts_per_day > 10:
        score -= 3
        reasons.append(f"posting burst ({c.feed_max_posts_per_day} posts in one day)")
    elif c.feed_max_posts_per_day > 5:
        score -= 2
        reasons.append(f"posting burst ({c.feed_max_posts_per_day} posts in one day)")

    # Small bonus for a named author.
    if AUTHOR_MARKUP.search(html):
        score += 0.5
        reasons.append("author markup")

    return QualityResult(score=score, keep=score >= THRESHOLD, reasons=reasons)
