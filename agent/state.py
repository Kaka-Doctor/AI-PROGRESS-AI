"""Track posting state + recent stories (state.json in the repo root).

Two jobs:
  * once-per-day guard  — last_post_date (EAT calendar day)
  * story dedup memory  — fingerprints of stories covered recently, so the
    same announcement is not re-reported days in a row
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


def today_eat() -> str:
    return datetime.now(EAT).strftime("%Y-%m-%d")


def posted_today(data: dict, today: str | None = None) -> bool:
    if not data.get("last_video"):
        return False
    return str(data.get("last_post_date") or "") == (today or today_eat())


def load(path: Path | None = None, settings: Settings | None = None) -> dict:
    p = path or (Path(settings.state_file) if settings and settings.state_file
                 else STATE_FILE)
    default = {
        "completed": 0,
        "last_run": None,
        "last_video": None,
        "last_post_date": None,
        "recent_stories": [],
        "note": "Daily AI news episodes. recent_stories keeps covered-story "
                "fingerprints for dedup (capped).",
    }
    if not p.exists():
        log.info("No state file — starting fresh.")
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("recent_stories", [])
        data.setdefault("completed", 0)
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("state.json unreadable (%s); starting fresh", exc)
        return default


def known_fingerprints(data: dict, lookback_days: int) -> set[str]:
    """Fingerprints of stories covered in the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
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
    data["recent_stories"] = data["recent_stories"][-200:]


def advance(path: Path | None, video_url: str, video_id: str,
            stories: list, settings: Settings | None = None) -> dict:
    p = path or (Path(settings.state_file) if settings and settings.state_file
                 else STATE_FILE)
    data = load(p, settings)
    data.update({
        "completed": int(data.get("completed", 0)) + 1,
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_video": {"id": video_id, "url": video_url},
        "last_post_date": today_eat(),
    })
    remember_stories(data, stories)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info("State advanced → episode #%d live at %s (dedup memory: %d)",
             data["completed"], video_url, len(data["recent_stories"]))
    return data
