"""One thumbnail per post: pick, download, sanitize, upload to Azure Blob.

Images are hostile input, so:
  * only JPEG / PNG / WebP / GIF are accepted (never SVG: it can carry script),
  * downloads go through the same home-network guard as every other request
    and are capped in size,
  * Pillow's pixel limit stops "decompression bomb" images,
  * everything is decoded and re-encoded as a small WebP, which strips metadata
    (EXIF, GPS) and anything else that isn't pixels.

Blob names are the SHA-256 of the WebP bytes, so identical images are stored
once and every URL can be cached forever.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
import lxml.html
from PIL import Image

from .config import CFG
from .net import BackOff, BlockedURL

Image.MAX_IMAGE_PIXELS = 40_000_000  # ~6300x6300; bigger is rejected before decoding

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "MPO"}  # MPO = multi-picture JPEG from phones
SKIP_HINTS = re.compile(
    r"(avatar|gravatar|logo|icon|favicon|sprite|emoji|badge|pixel|tracking|spacer|"
    r"banner|/ads?/|doubleclick|feeds\.feedburner|wp-includes)",
    re.I,
)
MAX_IMAGE_BYTES = 5_000_000
MIN_SOURCE_WIDTH = 200
THUMB_BOX = (320, 320)


@dataclass
class Thumbnail:
    source_url: str
    alt: str | None
    data: bytes          # WebP bytes
    width: int
    height: int
    sha256: str

    @property
    def blob_path(self) -> str:
        return f"{self.sha256[:2]}/{self.sha256}.webp"


def images_enabled() -> bool:
    return bool(CFG.blob_base_url and CFG.blob_sas)


def _robots_meta_blocks_images(doc) -> bool:
    for content in doc.xpath(
        '//meta[translate(@name,"ROBOTS","robots")="robots" or @name="AntiSEOBot"]/@content'
    ):
        c = content.lower()
        if "noimageindex" in c or "noindex" in c or "none" in c:
            return True
    return False


def _int_attr(el, name: str) -> int | None:
    v = (el.get(name) or "").strip().removesuffix("px")
    return int(v) if v.isdigit() else None


def pick(html: str, page_url: str) -> tuple[str, str | None] | None:
    """Return (image URL, alt text) for the post, or None.

    Preference: the author's chosen preview image (og:image / twitter:image),
    then the first reasonably large picture inside the article.
    """
    if not html:
        return None
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return None
    if _robots_meta_blocks_images(doc):
        return None

    meta = doc.xpath(
        '//meta[@property="og:image" or @name="og:image" or @property="og:image:url" '
        'or @name="twitter:image" or @property="twitter:image"]/@content'
    )
    alt_meta = doc.xpath('//meta[@property="og:image:alt" or @name="twitter:image:alt"]/@content')
    for url in meta:
        full = urljoin(page_url, url.strip())
        if _acceptable_url(full):
            return full, (alt_meta[0].strip() if alt_meta else None)

    for img in doc.xpath("//article//img | //main//img | //img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src:
            continue
        w, h = _int_attr(img, "width"), _int_attr(img, "height")
        if (w is not None and w < MIN_SOURCE_WIDTH) or (h is not None and h < 100):
            continue
        full = urljoin(page_url, src.strip())
        if _acceptable_url(full):
            alt = (img.get("alt") or "").strip() or None
            return full, alt
    return None


def _acceptable_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False  # also rules out data: URIs
    path = parts.path.lower()
    if path.endswith((".svg", ".svgz", ".ico", ".bmp", ".tif", ".tiff")):
        return False
    return not SKIP_HINTS.search(url)


def fetch_thumbnail(fetch, url: str, alt: str | None) -> Thumbnail | None:
    """Download and sanitize one image. Returns None on anything unexpected.

    `fetch(url)` returns an httpx.Response; the crawler passes a function that
    applies robots.txt and the per-site delay for same-site images.
    """
    try:
        resp = fetch(url)
    except (BlockedURL, BackOff, httpx.HTTPError):
        return None
    if resp.status_code != 200:
        return None
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_TYPES or len(resp.content) > MAX_IMAGE_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(resp.content)) as im:
            if im.format not in ALLOWED_FORMATS:
                return None
            if im.width < MIN_SOURCE_WIDTH or im.height < 100:
                return None
            im.draft("RGB", (THUMB_BOX[0] * 2, THUMB_BOX[1] * 2))  # fast JPEG downscale
            im.seek(0)  # first frame of animated GIF/WebP
            frame = im.convert("RGB")
            frame.thumbnail(THUMB_BOX, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            frame.save(out, "WEBP", quality=72, method=4)  # new file: no EXIF/ICC/XMP
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        return None
    data = out.getvalue()
    return Thumbnail(
        source_url=url,
        alt=(alt or None) and alt[:300],
        data=data,
        width=frame.width,
        height=frame.height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def upload(client: httpx.Client, thumb: Thumbnail) -> bool:
    return upload_status(client, thumb)[0]


def upload_status(client: httpx.Client, thumb: Thumbnail) -> tuple[bool, str]:
    """PUT the WebP into the container using the create-only SAS token.

    Returns (stored, detail). `If-None-Match: *` makes an existing blob a no-op:
    Azure answers 409 BlobAlreadyExists, which counts as stored. Any OTHER 409
    (e.g. PublicAccessNotPermitted, sent when the SAS is missing or broken) is a
    failure: treating every 409 as "exists" once recorded images that were never
    uploaded.
    """
    url = f"{CFG.blob_base_url.rstrip('/')}/{thumb.blob_path}?{CFG.blob_sas.lstrip('?')}"
    resp = client.put(
        url,
        content=thumb.data,
        headers={
            "x-ms-blob-type": "BlockBlob",
            "x-ms-blob-content-type": "image/webp",
            "x-ms-blob-cache-control": "public, max-age=31536000, immutable",
            "If-None-Match": "*",
        },
    )
    code = resp.headers.get("x-ms-error-code", "")
    if resp.status_code == 201:
        return True, "201"
    if resp.status_code == 409 and code == "BlobAlreadyExists":
        return True, "409 BlobAlreadyExists"
    return False, f"{resp.status_code} {code}".strip()
