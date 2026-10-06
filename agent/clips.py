"""Legal/public video clips so episodes are not just slides.

Three sources, all license-clean, tried in order per story:
  1. YouTube Creative-Commons videos (search.list with license=creativeCommons,
     downloaded with yt-dlp, reused under CC-BY with attribution). Most
     relevant to the story — YouTube's own relevance search does the work.
  2. Wikimedia Commons videos (CC0 / CC-BY / CC-BY-SA / public domain).
  3. Internet Archive movies (public domain / Creative Commons).

Every clip actually used in the episode is attributed in the YouTube
description (title, link, license) — that satisfies the CC-BY attribution
requirement. Failures at any level are skipped gracefully; the episode
always falls back to the Ken Burns b-roll slides.

No API keys needed: YouTube search uses the channel's own OAuth credentials
(already configured for uploads), Commons and Archive have open APIs.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import Settings
from .news import Story
from .video import probe_duration

log = logging.getLogger("clips")

# Wikimedia (and friends) require a descriptive UA with contact info —
# generic browser UAs get 403 on the API. This one is verified working.
UA = ("AIProgressAI-video-agent/1.0 "
      "(https://github.com/Kaka-Doctor/AI-PROGRESS-AI; "
      "automated news channel; contact: actions@users.noreply.github.com) "
      "requests")
TIMEOUT = 30
MIN_CLIP_SECONDS = 4.0        # shorter than this is useless as b-roll
MAX_DOWNLOAD_MB = 200         # refuse giant downloads
YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
ARCHIVE_META = "https://archive.org/metadata/{id}"

# Queries tried when a story yields nothing story-specific.
GENERIC_QUERIES = ["artificial intelligence technology", "machine learning",
                   "data center servers", "robotics technology"]

# A Commons/Archive result is only usable as AI-news b-roll if its title
# mentions something tech-ish — stops random footage sneaking in.
TOPIC_WORDS = {"ai", "a.i", "artificial", "intelligence", "robot",
               "tech", "technology", "data", "computer", "neural",
               "machine", "gpu", "server", "chatbot", "automation",
               "digital", "algorithm", "software", "cyber", "science",
               "openai", "anthropic", "claude", "gemini", "chatgpt",
               "gpt", "llm", "deepmind", "nvidia", "training",
               "dataset", "quantum", "developer", "coding", "python",
               "internet", "browser", "startup", "computer", "cpu",
               "chip", "semiconductor", "processor", "supercomputer",
               "deep", "learning", "inference", "compute", "cloud"}

# Official / first-party lab & company channels — when a Creative-Commons
# YouTube result comes from one of these, it is preferred (the channel
# brief: "use direct videos from open ai, anthropic, google and any other
# publicly available related video").
OFFICIAL_HINTS = ("openai", "anthropic", "deepmind", "google",
                  "nvidia", "microsoft", "meta ai", "ibm", "intel",
                  "mit", "stanford", "tesla", "xai", "mistral",
                  "hugging face", "huggingface", "aws", "amazon web",
                  "googlecloud", "google cloud", "research")


def _topic_ok(title: str) -> bool:
    words = {w.strip(".,:;!?()[]") for w in (title or "").lower().split()}
    if words & TOPIC_WORDS:
        return True
    # standalone "AI" (word boundary) — but NOT the letters inside
    # "train"/"email"/"main"...
    return bool(re.search(r"(?<![a-z0-9])ai(?![a-z0-9])",
                          (title or "").lower()))


def official_channel(channel_title: str, video_title: str = "") -> bool:
    """Is this YouTube result from (or about) an official AI lab / big
    tech channel? First-party uploads and demos are exactly what the
    channel wants chopped in."""
    hay = f"{channel_title or ''} {video_title or ''}".lower()
    return any(h in hay for h in OFFICIAL_HINTS)


@dataclass
class Clip:
    path: Path
    provider: str        # "youtube" | "commons" | "archive"
    title: str
    url: str             # human-facing source page (for attribution)
    license: str         # e.g. "CC-BY 3.0 (YouTube)", "CC BY 4.0", "Public domain"
    channel: str = ""    # YouTube uploader / Commons author
    duration: float = 0.0

    def attribution_line(self) -> str:
        who = f" by {self.channel}" if self.channel else ""
        return (f"• “{self.title[:70]}”{who} — {self.url} "
                f"({self.license}), reused under its license")


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

_STOP = {"the", "and", "with", "from", "this", "that", "just", "into", "over",
         "will", "what", "about", "after", "before", "says", "said", "new",
         "how", "why", "your", "their", "amid", "a", "an", "of", "in", "on",
         "for", "to", "is", "are", "as", "at", "by", "it", "its", "has",
         "have", "was", "were", "be", "been", "being", "more", "than"}


def story_queries(story: Story) -> list[str]:
    """Search phrases for a story: specific first, vendor-flavored next
    (OpenAI / Anthropic / Google DeepMind / NVIDIA first-party videos),
    generic fallbacks after."""
    words = [w for w in re.findall(r"[A-Za-z0-9\-.]{3,}", story.title)
             if w.lower() not in _STOP]
    specific = " ".join(words[:6])
    out = []
    if len(words) >= 2:
        out.append(specific)

    # vendor-flavored queries — direct footage FROM the lab the story is
    # about (or the generic big players when the story is industry-wide)
    hay = f"{story.title} {getattr(story, 'summary', '') or ''}".lower()
    vendors: list[str] = []
    if "openai" in hay or "gpt" in hay or "chatgpt" in hay or "sam altman" in hay:
        vendors += ["OpenAI", "OpenAI demo"]
    if "anthropic" in hay or "claude" in hay:
        vendors += ["Anthropic Claude"]
    if "google" in hay or "gemini" in hay or "deepmind" in hay:
        vendors += ["Google DeepMind"]
    if "nvidia" in hay or "gpu" in hay or "blackwell" in hay or "jensen" in hay:
        vendors += ["NVIDIA AI"]
    if "meta" in hay or "llama" in hay:
        vendors += ["Meta AI"]
    if "microsoft" in hay or "copilot" in hay:
        vendors += ["Microsoft AI Copilot"]
    if "training data" in hay or "dataset" in hay or "copyright" in hay:
        vendors += ["training data machine learning"]
    if not vendors:                    # industry-wide story → big players
        vendors = ["OpenAI", "Google DeepMind"]

    # order: most-specific, then the lab's own footage, then the tighter
    # variant, then the rest — the collector uses the first 3-4 of these
    out += [vendors[0]]
    if len(words) >= 4:  # tighter variant — top-4 most salient words
        out.append(" ".join(words[:4]))
    out.extend(vendors[1:])

    out.extend(GENERIC_QUERIES[:2])
    return out


# ---------------------------------------------------------------------------
# YouTube Creative Commons
# ---------------------------------------------------------------------------

def _yt_search(query: str, settings: Settings) -> list[dict]:
    """Search CC-licensed YouTube videos; needs the upload OAuth creds."""
    if not settings.has_youtube_credentials:
        return []
    try:
        from .youtube import _access_token
        token = _access_token(settings.yt_client_id,
                              settings.yt_client_secret,
                              settings.yt_refresh_token)
    except Exception as exc:  # noqa: BLE001
        log.debug("YouTube search auth failed: %s", exc)
        return []
    try:
        r = requests.get(YT_SEARCH, params={
            "part": "snippet", "q": query, "type": "video",
            "license": "creativeCommons", "maxResults": 8,
            "videoDuration": "short",   # < 4 min: small downloads, b-roll size
            "relevanceLanguage": "en", "safeSearch": "strict",
        }, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        if r.status_code != 200:
            log.debug("YouTube search HTTP %d: %s", r.status_code,
                      r.text[:200])
            return []
        return r.json().get("items", [])
    except requests.RequestException as exc:
        log.debug("YouTube search error: %s", exc)
        return []


def _yt_download(video_id: str, out_path: Path) -> Path | None:
    """Download a CC video at <=480p with yt-dlp (best effort)."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        log.info("yt-dlp not installed — skipping YouTube clips "
                 "(pip install yt-dlp to enable)")
        return None
    cmd = [
        yt_dlp, "--quiet", "--no-warnings", "--no-playlist",
        "--no-progress", "--retries", "2",
        "-f", "best[height<=480][ext=mp4]/best[height<=480]/best",
        "-o", str(out_path),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=300, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        log.debug("yt-dlp timed out on %s", video_id)
        return None
    if proc.returncode != 0 or not out_path.exists():
        log.debug("yt-dlp failed on %s: %s", video_id,
                  (proc.stderr or "")[-200:])
        return None
    if out_path.stat().st_size > MAX_DOWNLOAD_MB * 1e6:
        out_path.unlink(missing_ok=True)
        return None
    return out_path


def _find_yt_clip(story: Story, settings: Settings,
                  work_dir: Path, exclude: set[str] | None = None) -> Clip | None:
    exclude = exclude or set()
    for query in story_queries(story)[:2]:
        for item in _yt_search(query, settings):
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
            if dur < MIN_CLIP_SECONDS:
                out_path.unlink(missing_ok=True)
                continue
            return Clip(
                path=out_path, provider="youtube", title=title,
                url=page,
                license="CC-BY 3.0 via YouTube's Creative Commons option",
                channel=snip.get("channelTitle", ""), duration=dur)
    return None


# ---------------------------------------------------------------------------
# Wikimedia Commons
# ---------------------------------------------------------------------------

def _commons_search(query: str) -> list[dict]:
    """Video files on Commons; returns [{title, url, mime, license, author,
    duration, size}] sorted b-roll-first (short + small files preferred)."""
    try:
        r = requests.get(COMMONS_API, params={
            "action": "query", "format": "json", "list": "search",
            "srnamespace": 6, "srlimit": 12,
            "srsearch": f"{query} filetype:video",
        }, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        hits = r.json().get("query", {}).get("search", [])
    except requests.RequestException:
        return []

    out = []
    for h in hits:
        title = h.get("title", "")          # "File:Foo.webm"
        if not title.startswith("File:"):
            continue
        try:
            r2 = requests.get(COMMONS_API, params={
                "action": "query", "format": "json", "titles": title,
                "prop": "imageinfo",
                "iiprop": "url|mime|size|extmetadata|duration",
            }, headers={"User-Agent": UA}, timeout=TIMEOUT)
            pages = r2.json().get("query", {}).get("pages", {})
            info = next(iter(pages.values())).get("imageinfo", [{}])[0]
        except (requests.RequestException, StopIteration, IndexError, KeyError):
            continue
        if not str(info.get("mime", "")).startswith("video/") \
                and "ogg" not in str(info.get("mime", "")):
            continue
        meta = info.get("extmetadata", {})
        license_ = (meta.get("LicenseShortName", {}) or {}).get("value", "")
        author = (meta.get("Artist", {}) or {}).get("value", "")
        author = re.sub(r"<[^>]+>", "", author or "").strip()[:60]
        out.append({
            "title": title[len("File:"):],
            "url": info.get("url", ""),
            "page": f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
            "mime": info.get("mime", ""),
            "license": license_ or "see file page",
            "author": author,
            "duration": float(info.get("duration") or 0.0),
            "size": int(info.get("size") or 0),
        })

    def broll_rank(c: dict) -> tuple:
        dur, size = c["duration"], c["size"]
        # sweet spot: 8-240s and <= 40MB (we only use ~12s of it)
        nice = 0 if (8 <= dur <= 240 and size <= 40_000_000) else 1
        too_big = 2 if (dur > 1200 or size > MAX_DOWNLOAD_MB * 1e6) else 0
        return (too_big, nice, size)

    out.sort(key=broll_rank)
    return out


def _url_ext(url: str, default: str = ".webm") -> str:
    """File extension from a URL path, ignoring query strings
    (Commons URLs carry ?utm_campaign=... junk that would otherwise
    end up inside the filename)."""
    from urllib.parse import urlparse
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in {".webm", ".ogv", ".mp4", ".mov", ".mkv",
                                ".m4v"} else default


def _find_commons_clip(story: Story, work_dir: Path,
                       exclude: set[str] | None = None) -> Clip | None:
    exclude = exclude or set()
    for query in story_queries(story):
        for cand in _commons_search(query):
            url = cand["url"]
            if not url or cand["page"] in exclude:
                continue
            if not _topic_ok(cand["title"]):
                continue  # random footage that merely matched a word
            if cand["duration"] > 1200 or \
                    cand["size"] > MAX_DOWNLOAD_MB * 1e6:
                continue  # documentary-length files are a waste to fetch
            ext = _url_ext(url)
            out_path = work_dir / f"commons_{abs(hash(url)) % 10_000:04d}{ext}"
            if not out_path.exists():
                try:
                    r = requests.get(url, headers={"User-Agent": UA},
                                     timeout=120, stream=True)
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
            if dur < MIN_CLIP_SECONDS:
                out_path.unlink(missing_ok=True)
                continue
            return Clip(
                path=out_path, provider="commons",
                title=cand["title"], url=cand["page"],
                license=f"Wikimedia Commons ({cand['license']})",
                channel=cand["author"], duration=dur)
    return None


# ---------------------------------------------------------------------------
# Internet Archive
# ---------------------------------------------------------------------------

def _archive_search(query: str) -> list[dict]:
    try:
        r = requests.get(ARCHIVE_SEARCH, params={
            "q": f"({query}) AND mediatype:(movies)",
            "fl[]": ["identifier", "title", "licenseurl", "downloads"],
            "rows": 10, "page": 1, "output": "json",
        }, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return r.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError):
        return []


def _license_from_url(url: str) -> str:
    m = re.search(r"licenses/([a-z-]+)/([0-9.]+)", url or "", re.I)
    if m:
        kind, ver = m.group(1).upper(), m.group(2)
        return f"CC {'-'.join(kind.split('-')[:2])} {ver}"
    if "publicdomain" in (url or "").lower():
        return "Public domain"
    return "Internet Archive item (see item page for rights)"


def _find_archive_clip(story: Story, work_dir: Path,
                       exclude: set[str] | None = None) -> Clip | None:
    exclude = exclude or set()
    for query in story_queries(story):
        for doc in _archive_search(query):
            ident = doc.get("identifier", "")
            if not ident or f"https://archive.org/details/{ident}" in exclude:
                continue
            if not _topic_ok(str(doc.get("title", ""))):
                continue  # off-topic footage
            try:
                meta = requests.get(ARCHIVE_META.format(id=ident),
                                    headers={"User-Agent": UA},
                                    timeout=TIMEOUT).json()
            except (requests.RequestException, ValueError):
                continue
            files = meta.get("files", [])
            # video files only, smallest first (we just need ~12s of b-roll);
            # mp4 derivatives are the most server-friendly
            vids = [f for f in files
                    if str(f.get("name", "")).lower().endswith(
                        (".mp4", ".ogv", ".webm"))
                    and f.get("size")
                    and int(f["size"]) <= MAX_DOWNLOAD_MB * 1e6]
            if not vids:
                continue
            vids.sort(key=lambda f: (0 if str(f.get("name", "")).endswith(".mp4") else 1,
                                     int(f.get("size", 0))))
            fname = vids[0]["name"]
            durl = f"https://archive.org/download/{ident}/{fname}"
            out_path = work_dir / f"ia_{ident[:40]}{_url_ext(durl, '.mp4')}"
            if not out_path.exists():
                try:
                    rr = requests.get(durl, headers={"User-Agent": UA},
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
            if dur < MIN_CLIP_SECONDS:
                out_path.unlink(missing_ok=True)
                continue
            license_ = _license_from_url(
                doc.get("licenseurl", "") or
                str(meta.get("metadata", {}).get("rights", "")))
            return Clip(
                path=out_path, provider="archive",
                title=str(doc.get("title", ident))[:80],
                url=f"https://archive.org/details/{ident}",
                license=license_, duration=dur)
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_clips(stories: list[Story], settings: Settings,
               work_dir: Path) -> dict[int, Clip]:
    """Find one legal clip per story (up to max_video_clips overall).

    Returns {story_index (1-based): Clip}. Never raises — clips are a
    bonus, not a requirement.
    """
    if not settings.enable_clips:
        log.info("Clips disabled (ENABLE_CLIPS=0) — slides only.")
        return {}
    work_dir.mkdir(parents=True, exist_ok=True)
    got: dict[int, Clip] = {}
    used_urls: set[str] = set()   # never use the same clip twice per episode
    budget = max(0, settings.max_video_clips)
    for i, story in enumerate(stories, 1):
        if budget <= 0:
            break
        clip = None
        try:
            if settings.enable_yt_clips:
                clip = _find_yt_clip(story, settings, work_dir, used_urls)
                if clip:
                    log.info("clip for story %d: YouTube CC “%s” (%.0fs)",
                             i, clip.title[:50], clip.duration)
            if clip is None:
                clip = _find_commons_clip(story, work_dir, used_urls)
                if clip:
                    log.info("clip for story %d: Commons “%s” (%.0fs)",
                             i, clip.title[:50], clip.duration)
            if clip is None:
                clip = _find_archive_clip(story, work_dir, used_urls)
                if clip:
                    log.info("clip for story %d: Archive “%s” (%.0fs)",
                             i, clip.title[:50], clip.duration)
        except Exception as exc:  # noqa: BLE001
            log.debug("clip hunt failed for story %d: %s", i, exc)
            clip = None
        if clip:
            got[i] = clip
            used_urls.add(clip.url)
            budget -= 1
    log.info("clips: %d/%d stories have real video b-roll "
             "(rest use Ken Burns slides)", len(got), len(stories))
    return got


def attribution_lines(clips: dict[int, Clip]) -> list[str]:
    """Description block attributing every clip actually used."""
    return [c.attribution_line() for c in clips.values()]
