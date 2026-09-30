"""Best-effort b-roll: fetch each story's og:image from the article page.

These are the labs' own announcement images / article hero shots — real
visuals tied to the real news. Failures are silently skipped (procedural
slides are always the fallback), so a picky CDN can never break the run.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests
from PIL import Image

from .config import BROLL_DIR, Settings
from .news import Story

log = logging.getLogger("broll")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 20
MIN_W, MIN_H = 640, 360
MAX_BYTES = 15 * 1024 * 1024

OG_RE = re.compile(
    r'<meta[^>]+property=["\'](?:og:image|twitter:image)(?::secure_url)?["\']'
    r'[^>]+content=["\']([^"\']+)["\']', re.I)
OG_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'property=["\'](?:og:image|twitter:image)(?::secure_url)?["\']', re.I)


def _extract_image_url(html: str) -> str:
    for regex in (OG_RE, OG_RE2):
        m = regex.search(html)
        if m:
            return m.group(1).replace("&amp;", "&")
    return ""


def fetch_story_image(story: Story, index: int,
                      work_dir: Path | None = None) -> Path | None:
    """Download the article's hero image; returns a local path or None."""
    out_dir = work_dir or BROLL_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"story_{index:02d}.jpg"

    if out_path.exists() and out_path.stat().st_size > 20_000:
        return out_path  # cached from an earlier attempt this run

    try:
        r = requests.get(story.url, timeout=TIMEOUT, headers={
            "User-Agent": UA, "Accept": "text/html,*/*"})
        if r.status_code != 200 or not r.text:
            return None
        img_url = _extract_image_url(r.text[:400_000])
        if not img_url:
            return None
        if img_url.startswith("/"):
            from urllib.parse import urljoin
            img_url = urljoin(story.url, img_url)

        ir = requests.get(img_url, timeout=TIMEOUT, headers={
            "User-Agent": UA, "Referer": story.url})
        if ir.status_code != 200 or len(ir.content) < 15_000:
            return None
        if len(ir.content) > MAX_BYTES:
            return None

        with open(out_path, "wb") as fh:
            fh.write(ir.content)
        img = Image.open(out_path)
        if img.width < MIN_W or img.height < MIN_H:
            out_path.unlink(missing_ok=True)
            return None
        log.info("b-roll ok: story %d (%s) %dx%d", index, story.domain,
                 img.width, img.height)
        return out_path
    except Exception as exc:  # noqa: BLE001
        log.debug("b-roll failed for story %d: %s", index, exc)
        out_path.unlink(missing_ok=True)
        return None


def fetch_all(stories: list[Story], settings: Settings,
              work_dir: Path | None = None) -> dict[int, Path]:
    """Fetch images for the chosen stories; returns {story_index: path}."""
    got: dict[int, Path] = {}
    for i, s in enumerate(stories, 1):
        p = fetch_story_image(s, i, work_dir)
        if p:
            got[i] = p
    log.info("b-roll: %d/%d story images fetched", len(got), len(stories))
    return got
