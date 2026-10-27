"""Clean text extraction with trafilatura."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import lxml.html
import trafilatura

_STOPWORDS = set(
    "the of and to a in is that it for on with as was this be are by at from "
    "or an have not but you i we they he she his her their our".split()
)


@dataclass
class Extracted:
    title: str | None
    text: str
    date: datetime | None


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract(html: str, url: str) -> Extracted | None:
    """Main article text + metadata from a full page or a feed's HTML content."""
    if not html:
        return None
    raw = trafilatura.extract(
        html,
        url=url,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if raw:
        data = json.loads(raw)
        text = (data.get("text") or "").strip()
        if text:
            return Extracted(data.get("title"), text, _parse_date(data.get("date")))
    # Feed fragments are sometimes too small for trafilatura's heuristics.
    try:
        text = lxml.html.fromstring(html).text_content().strip()
    except Exception:
        return None
    return Extracted(None, re.sub(r"\s+\n", "\n", text), None) if text else None


def word_count(text: str) -> int:
    return len(text.split())


def looks_english(text: str) -> bool:
    """Cheap language check: English text is ~20-30% common stopwords."""
    words = re.findall(r"[a-zA-Z']+", text.lower())[:3000]
    if len(words) < 50:
        return False
    return sum(w in _STOPWORDS for w in words) / len(words) > 0.12
