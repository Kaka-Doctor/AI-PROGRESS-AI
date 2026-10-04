"""REAL video footage pool — the visual backbone of every episode.

The channel brief: ~90% real moving video, a few short moving slides for
headlines. This module collects license-clean tech/AI b-roll:

  per story   → story-specific queries (topical footage first)
  then        → generic AI/tech b-roll (data centers, robots, chips...)

Sources (all legal, every used one attributed in the description):
  1. Wikimedia Commons videos (CC0 / CC-BY / CC-BY-SA / public domain)
  2. Internet Archive movies (public domain / Creative Commons)
  3. YouTube Creative-Commons (CC-BY; yt-dlp is often bot-blocked on
     datacenter IPs, so it is tried LAST and failures are skipped fast)

From each source video several SEGMENTS are cut at varied offsets —
episodes are chopped together from many real videos, never one long one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .clips import (UA, _archive_search, _commons_search,
                    _license_from_url, _topic_ok, _url_ext, _yt_download,
                    _yt_search, story_queries)
from .config import Settings
from .news import Story
from .video import probe_duration

log = logging.getLogger("footage")

TIMEOUT = 30
MIN_SOURCE_SECONDS = 5.0
MAX_DOWNLOAD_MB = 200

# Wider generic pool: every query is run only when the story-specific ones
# cannot fill the episode — variety across episodes comes from rotation.
GENERIC_POOL = [
    "data center servers", "server room technology",
    "artificial intelligence technology", "machine learning",
    "robotics technology", "robot arm factory",
    "microchip semiconductor manufacturing", "computer processor",
    "self-driving car", "drone technology flying",
    "computer programming screen", "neural network visualization",
    "laboratory science research", "supercomputer",
]


def _segments_for(total: float, seg_len: float,
                  max_segments: int) -> list[tuple[float, float]]:
    """(start, seconds) pairs across a source: skip the intro, keep an end
    margin, each capped at seg_len; only segments that fully fit are kept."""
    lo = total * 0.05
    usable_end = total * 0.97 - 0.3
    span = max(usable_end - lo, 0.0)
    n = max(1, min(max_segments, int(span // max(seg_len, 1.0)) or 1))
    if n == 1:
        offsets = [lo]
    else:
        step = (span - seg_len) / (n - 1)
        offsets = [lo + i * step for i in range(n)]
    out: list[tuple[float, float]] = []
    for off in offsets:
        take = min(seg_len, usable_end - off)
        if take >= 3.0:
            out.append((round(off, 2), round(take, 2)))
    return out


@dataclass
class FootageSource:
    """One downloaded source video + the segments we cut from it."""
    path: Path
    provider: str           # "youtube" | "commons" | "archive"
    title: str
    url: str                # human-facing source page (attribution)
    license: str
    channel: str = ""
    duration: float = 0.0
    story: int = 0          # 1-based story index; 0 = generic tech b-roll
    segments: list[tuple[float, float]] = field(default_factory=list)

    @property
    def used_seconds(self) -> float:
        return sum(s for _, s in self.segments)

    def attribution_line(self) -> str:
        who = f" by {self.channel}" if self.channel else ""
        segs = ", ".join(f"{a:.0f}s+{b:.0f}s" for a, b in self.segments)
        return (f"• “{self.title[:70]}”{who} — {self.url} "
                f"({self.license}); segments used: {segs}. Reused under "
                f"its license with attribution.")


def _try_commons(query: str, work_dir: Path, exclude: set[str],
                 story: int, seg_len: float, max_seg: int
                 ) -> FootageSource | None:
    for cand in _commons_search(query):
        url = cand["url"]
        if not url or cand["page"] in exclude:
            continue
        if not _topic_ok(cand["title"]):
            continue          # random footage that merely matched a word
        if cand["duration"] > 1200 or cand["size"] > MAX_DOWNLOAD_MB * 1e6:
            continue
        ext = _url_ext(url)
        out_path = work_dir / f"commons_{abs(hash(url)) % 10_000:04d}{ext}"
        if not out_path.exists():
            try:
                r = requests.get(url, headers={"User-Agent": _UA()}, timeout=120,
                                 stream=True)
                if r.status_code != 200:
                    continue
                size = int(r.headers.get("content-length") or 0)
                if size > MAX_DOWNLOAD_MB * 1e6:
                    continue
                with open(out_path, "wb") as fh:
                    for chunk in r.iter_content(64 * 1024):
                        fh.write(chunk)
            except requests.RequestException:
                continue
        if out_path.stat().st_size > MAX_DOWNLOAD_MB * 1e6:
            out_path.unlink(missing_ok=True)
            continue
        try:
            dur = probe_duration(out_path)
        except Exception:  # noqa: BLE001
            out_path.unlink(missing_ok=True)
            continue
        if dur < MIN_SOURCE_SECONDS:
            out_path.unlink(missing_ok=True)
            continue
        return FootageSource(
            path=out_path, provider="commons", title=cand["title"],
            url=cand["page"], license=f"Wikimedia Commons ({cand['license']})",
            channel=cand["author"], duration=dur, story=story,
            segments=_segments_for(dur, seg_len, max_seg))
    return None


def _try_archive(query: str, work_dir: Path, exclude: set[str],
                 story: int, seg_len: float, max_seg: int
                 ) -> FootageSource | None:
    for doc in _archive_search(query):
        ident = doc.get("identifier", "")
        if not ident or f"archive.org/details/{ident}" in exclude:
            continue
        if not _topic_ok(str(doc.get("title", ""))):
            continue
        try:
            meta = requests.get(
                f"https://archive.org/metadata/{ident}",
                headers={"User-Agent": _UA()}, timeout=TIMEOUT).json()
        except (requests.RequestException, ValueError):
            continue
        files = meta.get("files", [])
        vids = [f for f in files
                if str(f.get("name", "")).lower().endswith(
                    (".mp4", ".ogv", ".webm"))
                and f.get("size")
                and 500_000 <= int(f["size"]) <= MAX_DOWNLOAD_MB * 1e6]
        if not vids:
            continue
        vids.sort(key=lambda f: (0 if str(f.get("name", "")).endswith(".mp4")
                                 else 1, int(f.get("size", 0))))
        fname = vids[0]["name"]
        durl = f"https://archive.org/download/{ident}/{fname}"
        out_path = work_dir / f"ia_{ident[:40]}{_url_ext(durl, '.mp4')}"
        if not out_path.exists():
            try:
                rr = requests.get(durl, headers={"User-Agent": _UA()},
                                  timeout=180, stream=True)
                if rr.status_code != 200:
                    continue
                with open(out_path, "wb") as fh:
                    for chunk in rr.iter_content(64 * 1024):
                        fh.write(chunk)
            except requests.RequestException:
                continue
        if out_path.stat().st_size > MAX_DOWNLOAD_MB * 1e6:
            out_path.unlink(missing_ok=True)
            continue
        try:
            dur = probe_duration(out_path)
        except Exception:  # noqa: BLE001
            out_path.unlink(missing_ok=True)
            continue
        if dur < MIN_SOURCE_SECONDS:
            out_path.unlink(missing_ok=True)
            continue
        license_ = _license_from_url(
            doc.get("licenseurl", "") or
            str(meta.get("metadata", {}).get("rights", "")))
        return FootageSource(
            path=out_path, provider="archive",
            title=str(doc.get("title", ident))[:80],
            url=f"https://archive.org/details/{ident}",
            license=license_, duration=dur, story=story,
            segments=_segments_for(dur, seg_len, max_seg))
    return None


def _try_youtube(query: str, settings: Settings, work_dir: Path,
                 exclude: set[str], story: int, seg_len: float, max_seg: int
                 ) -> FootageSource | None:
    """YouTube CC — tried last: yt-dlp is frequently bot-blocked from
    datacenter runners, and every blocked candidate burns minutes."""
    if not settings.enable_yt_clips or not settings.has_youtube_credentials:
        return None
    items = _yt_search(query, settings)
    for item in items[:4]:
        vid = item["id"].get("videoId", "")
        snip = item.get("snippet", {})
        title = snip.get("title", "")
        if not vid or not title:
            continue
        page = f"https://www.youtube.com/watch?v={vid}"
        if page in exclude:
            continue
        out_path = work_dir / f"yt_{vid}.mp4"
        if not out_path.exists():
            if _yt_download(vid, out_path) is None:
                continue
        try:
            dur = probe_duration(out_path)
        except Exception:  # noqa: BLE001
            out_path.unlink(missing_ok=True)
            continue
        if dur < MIN_SOURCE_SECONDS:
            out_path.unlink(missing_ok=True)
            continue
        return FootageSource(
            path=out_path, provider="youtube", title=title, url=page,
            license="CC-BY 3.0 via YouTube's Creative Commons option",
            channel=snip.get("channelTitle", ""), duration=dur, story=story,
            segments=_segments_for(dur, seg_len, max_seg))
    return None


def _UA() -> str:
    return UA


def collect_footage(stories: list[Story], settings: Settings,
                    work_dir: Path, needed_seconds: float,
                    per_story: int = 2) -> list[FootageSource]:
    """Collect real footage until the pool reaches needed_seconds.

    Story-specific footage first (each story gets up to `per_story`
    sources), generic AI/tech b-roll fills the remainder. Never raises —
    a thin pool simply means more slide time and a QA ratio decision.
    """
    if not settings.enable_footage:
        log.info("Footage disabled (ENABLE_FOOTAGE=0) — slides only.")
        return []
    work_dir.mkdir(parents=True, exist_ok=True)
    seg_len = settings.footage_segment_seconds
    max_seg = 3
    exclude: set[str] = set()
    sources: list[FootageSource] = []

    def _hunt(queries: list[str], story: int, limit: int) -> None:
        got = 0
        for query in queries:
            if got >= limit:
                return
            for finder in (_try_commons, _try_archive, _try_youtube):
                if got >= limit:
                    return
                try:
                    src = (finder(query, work_dir, exclude, story,
                                  seg_len, max_seg)
                           if finder is not _try_youtube else
                           finder(query, settings, work_dir, exclude,
                                  story, seg_len, max_seg))
                except Exception as exc:  # noqa: BLE001
                    log.info("finder %s failed on %r: %s",
                             finder.__name__, query, exc)
                    src = None
                if src is not None:
                    exclude.add(src.url)
                    sources.append(src)
                    got += 1
                    log.info("footage source %d (%s, story %d): “%s” "
                             "(%.0fs → %d segments)",
                             len(sources), src.provider, story,
                             src.title[:44], src.duration,
                             len(src.segments))

    total = 0.0

    def _pool_seconds() -> float:
        return sum(s.used_seconds for s in sources)

    # 1. story-specific footage (topical first)
    for idx, story in enumerate(stories, 1):
        _hunt(story_queries(story)[:3], idx, per_story)
    # 2. generic AI/tech b-roll until the pool covers the episode
    for query in GENERIC_POOL:
        if _pool_seconds() >= needed_seconds:
            break
        _hunt([query], 0, 2)
        if len(sources) >= settings.max_footage_sources:
            break

    total = _pool_seconds()
    log.info("FOOTAGE POOL: %d sources, %d segments, %.0fs of real video "
             "(needed %.0fs, %d%% coverage)",
             len(sources), sum(len(s.segments) for s in sources), total,
             needed_seconds, round(100 * total / max(needed_seconds, 1)))
    return sources


def attribution_lines(sources: list[FootageSource]) -> list[str]:
    """Description block attributing every source actually used."""
    return [s.attribution_line() for s in sources if s.segments]
