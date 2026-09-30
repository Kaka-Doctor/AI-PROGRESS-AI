"""Aggregate today's AI news from official lab feeds, tech media RSS, and
trending signals (Hacker News front page, Reddit).

No API keys required. Every source is fetched independently and failures
are skipped gracefully — a dead feed never kills the episode.

Ranking blends:
  * source weight       (official lab blogs rank above aggregators)
  * recency             (newer is better)
  * keyword salience    (model/lab names, release & benchmark language)
  * social traction     (HN points / Reddit score when available)
  * title overlap dedup (the same story from many outlets counts once)
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .config import FEEDS, HN_FRONT_PAGE_URL, REDDIT_FEEDS, Settings

log = logging.getLogger("news")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 25

# Salience keywords: model families, labs, and signal words.
KEYWORDS = {
    "gpt": 3, "chatgpt": 3, "o1": 2, "o3": 2, "o4": 2, "sora": 3,
    "claude": 3, "anthropic": 3,
    "gemini": 3, "deepmind": 3, "notebooklm": 2,
    "llama": 3, "meta ai": 3,
    "qwen": 3, "deepseek": 3, "kimi": 2, "moonshot": 2, "glm": 2,
    "zhipu": 2, "minimax": 2, "bytedance": 2, "seedance": 2, "ernie": 2,
    "mistral": 3, "grok": 3, "xai": 3, "copilot": 2, "midjourney": 2,
    "open source": 2, "open-source": 2, "weights": 2,
    "release": 2, "launch": 2, "launches": 2, "announc": 2, "unveil": 2,
    "benchmark": 2, "state of the art": 3, "sota": 2, "record": 1,
    "billion": 1, "funding": 1, "raise": 1, "acquisition": 1,
    "regulation": 1, "eu ai act": 2, "safety": 1, "agi": 2,
    "robot": 1, "agent": 1, "agents": 1, "video model": 2, "reasoning": 2,
    "ai": 1, "artificial intelligence": 2, "machine learning": 2,
    "neural network": 2, "llm": 2, "data center": 1, "datacenter": 1,
    "chip": 1, "gpu": 1, "nvidia": 2, "semiconductor": 1,
}


@dataclass
class Story:
    title: str
    url: str
    source: str
    published: datetime
    summary: str = ""
    weight: int = 0            # source weight 1-10
    official: bool = False     # straight from a lab's own blog
    social: int = 0            # HN points / reddit score bonus
    score: float = 0.0
    image_url: str = ""

    @property
    def domain(self) -> str:
        m = re.search(r"https?://(?:www\.)?([^/]+)", self.url)
        return m.group(1) if m else self.source

    @property
    def fingerprint(self) -> str:
        """Stable identity for dedup across days."""
        norm = re.sub(r"[^a-z0-9 ]", "", self.title.lower()).strip()
        return hashlib.sha1(norm.encode()).hexdigest()[:16]

    def age_hours(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.published
                         ).total_seconds() / 3600.0)


def _salience(title: str, summary: str = "") -> int:
    text = f"{title} {title} {summary}".lower()  # title counted twice
    return sum(w for kw, w in KEYWORDS.items() if kw in text)


def _strip_html(raw: str, limit: int = 900) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _parse_date(entry: dict | ET.Element, ns_atom: str = "") -> datetime | None:
    try:
        if isinstance(entry, dict):  # JSON (HN / Reddit-preparsed)
            for key in ("publishedISO", "updatedISO", "isoDate"):
                if entry.get(key):
                    return datetime.fromisoformat(
                        entry[key].replace("Z", "+00:00"))
        else:
            for tag in ("pubDate", "published", "updated", "date",
                        f"{{{ns_atom}}}published", f"{{{ns_atom}}}updated"):
                val = entry.findtext(tag)
                if val:
                    val = val.strip()
                    try:
                        return datetime.fromisoformat(val.replace("Z", "+00:00"))
                    except ValueError:
                        try:
                            dt = parsedate_to_datetime(val)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            return dt
                        except Exception:  # noqa: BLE001
                            continue
    except Exception:  # noqa: BLE001
        pass
    return None


def _fetch(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
        if r.status_code == 200 and r.content:
            return r.text
        log.debug("feed %s -> HTTP %s", url, r.status_code)
    except requests.RequestException as exc:
        log.debug("feed %s failed: %s", url, exc)
    return None


def _parse_one_feed(args: tuple) -> list[Story]:
    """Fetch + parse a single RSS feed (runs in a worker thread)."""
    label, url, weight, official, ai_filter = args
    raw = _fetch(url)
    if not raw:
        log.info("feed skipped (unreachable): %s", label)
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        log.warning("feed skipped (bad XML): %s", label)
        return []
    ns_atom = ""
    m = re.match(r"\{(.+)\}", root.tag)
    if m:  # atom
        ns_atom = m.group(1)
        entries = root.findall(f"{{{ns_atom}}}entry")
        item_title = f"{{{ns_atom}}}title"
        item_link = f"{{{ns_atom}}}link"
        item_sum = f"{{{ns_atom}}}summary"
        item_content = f"{{{ns_atom}}}content"
    else:
        channel = root.find("channel")
        entries = (channel.findall("item") if channel is not None
                   else root.findall(".//item"))
        item_title, item_link = "title", "link"
        item_sum, item_content = "description", "content:encoded"
    out: list[Story] = []
    for e in entries:
        title = (e.findtext(item_title) or "").strip()
        link = (e.findtext(item_link) or "").strip()
        if not link and ns_atom:
            le = e.find(item_link)
            link = (le.get("href") if le is not None else "") or ""
        if not title or not link:
            continue
        if ai_filter and _salience(title) < 2:
            continue  # general world feed: keep only AI-relevant headlines
        summary = _strip_html(e.findtext(item_sum)
                              or e.findtext(item_content) or "")
        published = _parse_date(e, ns_atom)
        if published is None:
            continue  # undatable items are useless for a daily episode
        out.append(Story(
            title=html.unescape(title), url=link, source=label,
            published=published, summary=summary,
            weight=weight, official=official))
    log.info("feed ok: %-22s %2d items", label, len(out))
    return out


def _rss_stories() -> list[Story]:
    """Fetch all feeds in parallel (6 workers) — 25 sources in ~10s."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_parse_one_feed, FEEDS))
    stories = [s for chunk in results for s in chunk]
    return stories


def _hn_stories() -> list[Story]:
    """Hacker News front page = strong 'trending right now' signal."""
    raw = _fetch(HN_FRONT_PAGE_URL)
    if not raw:
        return []
    try:
        hits = (raw and __import__("json").loads(raw).get("hits", [])) or []
    except Exception:  # noqa: BLE001
        return []
    out: list[Story] = []
    for h in hits:
        title = (h.get("title") or "").strip()
        url = (h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}")
        if not title:
            continue
        pts = int(h.get("points") or 0)
        if _salience(title) < 2:  # keep only AI-relevant front-page items
            continue
        published = _parse_date(h)
        if published is None:
            published = datetime.now(timezone.utc)
        out.append(Story(
            title=title, url=url, source="Hacker News",
            published=published, summary=_strip_html(
                (h.get("story_text") or "") or ""),
            weight=5, official=False, social=min(pts // 10, 30)))
    log.info("feed ok: %-22s %2d items (AI-filtered)", "Hacker News", len(out))
    return out


def _reddit_stories() -> list[Story]:
    out: list[Story] = []
    for label, url in REDDIT_FEEDS:
        raw = _fetch(url)
        if not raw:
            log.info("feed skipped (unreachable): %s", label)
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        for e in root.findall(".//entry"):
            title = (e.findtext(f"{{http://www.w3.org/2005/Atom}}title") or "")
            title = html.unescape(title).strip()
            link_e = e.find(f"{{http://www.w3.org/2005/Atom}}link")
            link = (link_e.get("href") if link_e is not None else "") or ""
            published = _parse_date(
                e, "http://www.w3.org/2005/Atom") or datetime.now(timezone.utc)
            if not title or not link:
                continue
            if _salience(title) < 3:
                continue
            m = re.search(r"r/(\w+)", title)
            if m:  # strip "r/MachineLearning - " prefix
                title = title.split(" - ", 1)[-1]
            out.append(Story(title=title, url=link, source=label,
                             published=published, summary="",
                             weight=4, official=False, social=6))
    log.info("feed ok: Reddit (%d AI threads)", len(out))
    return out


def collect_stories(settings: Settings,
                     window_hours: int | None = None) -> list[Story]:
    """Fetch everything, dedup, rank, and return stories inside the window.

    `window_hours` overrides settings.news_window_hours so callers can
    widen the net when the fresh supply is thin (missed-news coverage).
    """
    window = window_hours or settings.news_window_hours
    stories = _rss_stories() + _hn_stories() + _reddit_stories()
    log.info("collected %d raw items from the web", len(stories))
    if not stories:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window)
    fresh = [s for s in stories if s.published >= cutoff]
    log.info("%d items inside the %dh window", len(fresh), window)

    # Cross-outlet dedup on fuzzy title overlap.
    ranked: list[Story] = []
    for s in sorted(fresh, key=lambda x: (x.weight, x.social), reverse=True):
        if any(_similar(s.title, r.title) for r in ranked):
            continue
        ranked.append(s)

    for s in ranked:
        age_h = s.age_hours()
        recency = max(0.0, 20.0 - age_h / 3.0)          # 0h->20, 48h->4
        s.score = (s.weight * 4.0
                   + recency
                   + _salience(s.title, s.summary)
                   + s.social
                   + (5.0 if s.official else 0.0))
    ranked.sort(key=lambda s: s.score, reverse=True)
    return ranked


def _similar(a: str, b: str) -> bool:
    """Cheap Jaccard word overlap for near-duplicate headlines."""
    wa = {w for w in re.findall(r"[a-z0-9]{4,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z0-9]{4,}", b.lower())}
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) > 0.62


def format_digest(stories: list[Story],
                  follow_ups: set[int] | None = None) -> str:
    """Human-readable digest used for logs and the Gemini prompt.

    Stories whose 1-based index is in `follow_ups` were already covered in a
    recent episode — the digest marks them so the writer frames them as an
    UPDATE (new angle), never an exact repeat.
    """
    follow_ups = follow_ups or set()
    lines = []
    for i, s in enumerate(stories, 1):
        when = s.published.strftime("%b %d, %H:%M UTC")
        marker = ("\n  [FOLLOW-UP: this story was already covered in an "
                  "earlier episode — frame it as a fresh UPDATE with a NEW "
                  "angle and new details; do NOT repeat the earlier framing]"
                  if i in follow_ups else "")
        lines.append(
            f"STORY {i}: {s.title}\n"
            f"  source: {s.source}{' (official lab blog)' if s.official else ''}"
            f" | published: {when} | url: {s.url}{marker}\n"
            f"  summary: {s.summary or '(no summary provided — rely on the title)'}")
    return "\n".join(lines)
