r"""Anti-SEO quality gate.  >>> THIS FILE IS YOURS TO WRITE <<<

Every post that survives extraction (English, >= MIN_WORDS words) comes through
`assess()`. Return a score, whether to keep it, and the reasons, so the crawl
log can explain every decision ("dropped: 4 ad scripts, listicle title").

Until you fill it in, everything is kept.

What you get (see models.Candidate):
  c.title, c.text           clean title and article text
  c.html                    raw HTML (full page if c.html_is_full_page, else the
                            feed's HTML content, which has no <script> tags)
  c.url, c.host
  c.feed_max_posts_per_day  most posts this feed published on a single day

Signals from the plan (§4.4). Suggested weights are a starting point only:
  + has a feed                     every seeded domain does, so it is implicit
  - ad / affiliate scripts         count googlesyndication, doubleclick,
                                   amazon-adsystem, taboola, outbrain in c.html,
                                   plus links with ?tag= (Amazon affiliate)
                                   -> maybe -1 per hit
  - listicle / SEO title           r"\btop \d+\b", r"\bbest .* in 20\d\d\b",
                                   r"ultimate guide", r"you need to know"
                                   -> maybe -1.5
  - LLM boilerplate in the text    "in today's fast-paced", "delve into",
                                   "as an ai", "in conclusion,", "it's important to note"
                                   -> maybe -0.5 per phrase
  - posting burst                  feed_max_posts_per_day > 5 -> maybe -2
  + author / about page            needs an extra request per domain, so skip
                                   it for now (or check c.html for rel="author")

Tune THRESHOLD with your 10 test queries: run a crawl, run
`search --bench queries.txt`, look at what got in, adjust, repeat.
Unit-test it with a few hand-made Candidates; it is pure logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Candidate

THRESHOLD = 0.0  # keep if score >= THRESHOLD


@dataclass
class QualityResult:
    score: float
    keep: bool
    reasons: list[str] = field(default_factory=list)


def assess(c: Candidate) -> QualityResult:
    score = 0.0
    reasons: list[str] = []

    # TODO(you): ad/affiliate scripts in c.html
    # TODO(you): listicle / SEO title patterns in c.title
    # TODO(you): LLM-boilerplate phrases in c.text
    # TODO(you): posting burst from c.feed_max_posts_per_day

    return QualityResult(score=score, keep=score >= THRESHOLD, reasons=reasons)
