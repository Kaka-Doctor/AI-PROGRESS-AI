"""Track posting state + covered stories (state.json in the repo root).

Three jobs:
  * twice-a-day guard   — the channel posts every ~12 hours; a new episode
    is allowed only after MIN_HOURS_BETWEEN_POSTS (default 10.5, tolerant
    of GitHub cron delays). `post_log` keeps the recent upload timestamps.
  * story dedup memory  — fingerprints of stories covered recently, so the
    same announcement is not re-reported episode after episode. Stories
    covered within DEDUP_LOOKBACK_HOURS are excluded; if supply is thin
    they may return as explicit FOLLOW-UPs (new angle, never an exact
    repeat).
  * metadata            — episode counter, last video, last run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import STATE_FILE, Settings

log = logging.getLogger("state")

# The channel's posting day runs on East Africa Time (UTC+3).
EAT = timezone(timedelta(hours=3))

MAX_POST_LOG = 30      # recent uploads remembered for the cadence guard
MAX_STORIES = 200      # fingerprint memory cap


def today_eat() -> str:
    return datetime.now(EAT).strftime("%Y-%m-%d")


def posted_today(data: dict, today: str | None = None) -> bool:
    """Legacy helper: True when an episode went live this EAT day."""
    if not data.get("last_video"):
        return False
    return str(data.get("last_post_date") or "") == (today or today_eat())


def hours_since_last_post(data: dict) -> float | None:
    """Hours since the most recent upload, or None if never posted."""
    log_entries = data.get("post_log") or []
    if log_entries:
        try:
            last = datetime.fromisoformat(
                str(log_entries[-1].get("at", "")).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - last).total_seconds() / 3600
        except ValueError:
            pass
    # fall back to last_run (older state files)
    try:
        last = datetime.fromisoformat(
            str(data.get("last_run", "")).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600
    except ValueError:
        return None


def cadence_allows_post(data: dict, settings: Settings) -> tuple[bool, str]:
    """(allowed, reason) — the twice-a-day (every 12h) posting guard."""
    if not data.get("last_video"):
        return True, "first episode ever"
    gap = hours_since_last_post(data)
    if gap is None:
        return True, "last post time unknown — allowing"
    if gap >= settings.min_hours_between_posts:
        return True, f"{gap:.1f}h since the last episode (minimum " \
                     f"{settings.min_hours_between_posts}h)"
    return False, (f"only {gap:.1f}h since the last episode "
                   f"(minimum {settings.min_hours_between_posts}h) — the "
                   "channel posts every 12 hours, this slot is too early")


def load(path: Path | None = None, settings: Settings | None = None) -> dict:
    p = path or (Path(settings.state_file) if settings and settings.state_file
                 else STATE_FILE)
    default = {
        "completed": 0,
        "last_run": None,
        "last_video": None,
        "last_post_date": None,
        "post_log": [],
        "recent_stories": [],
        "note": "AI news episodes, twice a day (~every 12h). post_log keeps "
                "recent upload times for the cadence guard; recent_stories "
                "keeps covered-story fingerprints so missed news gets "
                "covered next episode without exact repeats.",
    }
    if not p.exists():
        log.info("No state file — starting fresh.")
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("recent_stories", [])
        data.setdefault("post_log", [])
        data.setdefault("completed", 0)
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("state.json unreadable (%s); starting fresh", exc)
        return default


def known_fingerprints(data: dict, lookback_hours: float) -> set[str]:
    """Fingerprints of stories covered in the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    known: set[str] = set()
    for s in data.get("recent_stories", []):
        try:
            when = datetime.fromisoformat(str(s.get("date", "")).replace(
                "Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff:
            known.add(str(s.get("fingerprint", "")))
    return known


def covered_recently(data: dict, story, lookback_hours: float) -> bool:
    """True when this exact story was covered within the lookback window."""
    return story.fingerprint in known_fingerprints(data, lookback_hours)


def remember_stories(data: dict, stories: list) -> None:
    now = datetime.now(timezone.utc)
    existing = {(s.get("fingerprint"), s.get("title")) for s in
                data.get("recent_stories", [])}
    for s in stories:
        key = (s.fingerprint, s.title)
        if key in existing:
            continue
        data.setdefault("recent_stories", []).append({
            "fingerprint": s.fingerprint,
            "title": s.title[:120],
            "url": s.url[:200],
            "source": s.source,
            "date": now.isoformat(timespec="seconds"),
        })
    data["recent_stories"] = data["recent_stories"][-MAX_STORIES:]


def advance(path: Path | None, video_url: str, video_id: str,
            stories: list, settings: Settings | None = None) -> dict:
    p = path or (Path(settings.state_file) if settings and settings.state_file
                 else STATE_FILE)
    data = load(p, settings)
    now = datetime.now(timezone.utc)
    data.update({
        "completed": int(data.get("completed", 0)) + 1,
        "last_run": now.isoformat(timespec="seconds"),
        "last_video": {"id": video_id, "url": video_url},
        "last_post_date": today_eat(),
    })
    data.setdefault("post_log", []).append({
        "at": now.isoformat(timespec="seconds"),
        "video_id": video_id,
        "url": video_url,
    })
    data["post_log"] = data["post_log"][-MAX_POST_LOG:]
    remember_stories(data, stories)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info("State advanced → episode #%d live at %s (dedup memory: %d, "
             "post log: %d)", data["completed"], video_url,
             len(data["recent_stories"]), len(data["post_log"]))
    return data
