"""Quality gate tests. Run from crawler/:  python -m pytest -q tests   (or: python tests/test_quality.py)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from antiseo_crawler.models import Candidate  # noqa: E402
from antiseo_crawler.quality import THRESHOLD, assess  # noqa: E402

HUMAN_TEXT = "I spent the weekend chasing a leak in our Rust service. " * 40


def cand(title="Chasing a leak in Rust", text=HUMAN_TEXT, html="", full=True, burst=1):
    return Candidate(domain_id=1, host="x.example", url="https://x.example/p", title=title, text=text,
                     published_at=None, html=html, html_is_full_page=full, feed_max_posts_per_day=burst)


def test_normal_post_is_kept_with_no_reasons():
    r = assess(cand())
    assert r.keep and r.score == 0 and r.reasons == []


def test_one_weak_signal_alone_does_not_drop():
    assert assess(cand(title="Top 10 Rust tips")).keep
    assert assess(cand(html='<script src="https://pagead2.googlesyndication.com/x.js"></script>')).keep


def test_seo_title_plus_ads_is_dropped():
    html = ('<script src="https://pagead2.googlesyndication.com/a.js"></script>'
            '<script src="https://cdn.taboola.com/b.js"></script>'
            '<a href="https://www.amazon.com/dp/B0?tag=farm-20">buy</a>')
    r = assess(cand(title="Best VPNs in 2026: The Ultimate Guide", html=html))
    assert not r.keep
    assert r.score < THRESHOLD
    assert any("listicle" in x for x in r.reasons) and any("ad/affiliate" in x for x in r.reasons)


def test_llm_boilerplate_and_burst_drop_a_content_farm():
    text = ("In today's fast-paced world, let's delve into the ever-changing landscape. "
            "It's important to note that this is a testament to progress. In conclusion, ") * 5 + HUMAN_TEXT
    r = assess(cand(text=text, burst=12))
    assert not r.keep
    assert any("boilerplate" in x for x in r.reasons) and any("burst" in x for x in r.reasons)


def test_author_markup_is_a_small_bonus():
    r = assess(cand(html='<a rel="author" href="/about">Jane</a>'))
    assert r.score == 0.5 and r.keep


def test_ad_penalty_is_capped():
    html = " ".join(["googlesyndication.com", "doubleclick.net", "amazon-adsystem.com", "taboola.com",
                     "outbrain.com", "ezoic.net", "mediavine.com"])
    assert assess(cand(html=html)).score == -4


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
